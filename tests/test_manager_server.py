import os
import socket
import sys
import threading
import time

import requests

from divebird.config import Settings
from divebird.engine.manager import DownloadManager
from divebird.models import Status, Task
from divebird.server import ApiServer


class Recorder:
    def __init__(self):
        self.added, self.removed, self.finished, self.changed = [], [], [], 0
        self.done = threading.Event()

    def task_added(self, t): self.added.append(t.id)
    def task_removed(self, tid): self.removed.append(tid)
    def task_changed(self, t): self.changed += 1

    def task_finished(self, t):
        self.finished.append(t.id)
        self.done.set()


def wait_for(cond, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_manager_queue_and_persistence(server, tmp_path):
    for i in range(4):
        server.add(f"/f{i}.bin", 1024 * 1024)
    settings = Settings(download_dir=str(tmp_path / "dl"), connections=4, max_concurrent=2)
    rec = Recorder()
    store = tmp_path / "tasks.json"
    mgr = DownloadManager(settings, rec, store=store)
    tasks = [mgr.add(Task(url=server.url(f"/f{i}.bin"))) for i in range(4)]
    assert wait_for(lambda: all(t.status == Status.COMPLETED for t in tasks))
    assert sorted(rec.finished) == sorted(t.id for t in tasks)
    assert all((tmp_path / "dl" / f"f{i}.bin").stat().st_size == 1024 * 1024 for i in range(4))
    mgr.shutdown()

    mgr2 = DownloadManager(settings, Recorder(), store=store)
    assert len(mgr2.tasks) == 4
    assert all(t.status == Status.COMPLETED for t in mgr2.tasks.values())
    mgr2.shutdown()


def test_manager_pause_resume(server, tmp_path):
    server.add("/big.bin", 8 * 1024 * 1024)
    server.throttle = 0.03
    settings = Settings(download_dir=str(tmp_path), connections=4)
    mgr = DownloadManager(settings, Recorder(), store=tmp_path / "tasks.json")
    t = mgr.add(Task(url=server.url("/big.bin")))
    assert wait_for(lambda: t.downloaded > 0)
    mgr.pause(t.id)
    assert t.status == Status.PAUSED
    assert wait_for(lambda: t.id not in mgr._runners)
    server.throttle = 0
    mgr.start(t.id)
    assert wait_for(lambda: t.status == Status.COMPLETED)
    assert (tmp_path / "big.bin").stat().st_size == 8 * 1024 * 1024
    mgr.shutdown()


def test_manager_error_status(server, tmp_path):
    settings = Settings(download_dir=str(tmp_path))
    mgr = DownloadManager(settings, Recorder(), store=tmp_path / "tasks.json")
    t = mgr.add(Task(url=server.url("/nope")))
    assert wait_for(lambda: t.status == Status.ERROR)
    assert "404" in t.error
    mgr.shutdown()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_api_server_security():
    port = _free_port()
    got, shown = [], []
    api = ApiServer(port, on_download=got.append, on_show=lambda: shown.append(1))
    assert api.start()
    base = f"http://127.0.0.1:{port}"
    try:
        r = requests.get(base + "/api/ping", timeout=5)
        assert r.status_code == 200 and r.json()["app"] == "Divebird"

        ext = {"Origin": "chrome-extension://abcdefghijklmnop"}
        r = requests.post(base + "/api/download", json={"url": "https://example.com/a.mp4"}, headers=ext, timeout=5)
        assert r.status_code == 200
        assert r.headers["Access-Control-Allow-Origin"] == ext["Origin"]
        assert got[-1]["url"] == "https://example.com/a.mp4"

        # 一般網頁不能呼叫
        r = requests.post(base + "/api/download", json={"url": "https://example.com/b.mp4"},
                          headers={"Origin": "https://evil.example"}, timeout=5)
        assert r.status_code == 403
        # DNS rebinding：Host 不是 127.0.0.1 / localhost
        r = requests.post(base + "/api/download", json={"url": "https://example.com/c.mp4"},
                          headers={"Host": f"evil.example:{port}"}, timeout=5)
        assert r.status_code == 403
        # 只接受 http(s)
        r = requests.post(base + "/api/download", json={"url": "file:///etc/passwd"}, headers=ext, timeout=5)
        assert r.status_code == 400
        assert len(got) == 1

        r = requests.post(base + "/api/show", json={}, timeout=5)
        assert r.status_code == 200 and shown == [1]
    finally:
        api.stop()
    # 埠被佔用時 start() 回傳 False
    blocker = socket.socket()
    if os.name != "nt":
        # Linux：剛關閉的埠還在 TIME_WAIT，需要 SO_REUSEADDR 才能綁定（仍不允許兩個 listener 共用）
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    try:
        assert ApiServer(port, got.append, lambda: None).start() is False
    finally:
        blocker.close()


def test_concurrent_tasks_with_same_name_get_distinct_files(server, tmp_path):
    """兩個同名下載同時進行時，各自要有自己的檔名與暫存檔，不可互相覆蓋。"""
    a = server.add("/a/file.bin", 3 * 1024 * 1024)
    b = server.add("/b/file.bin", 3 * 1024 * 1024)
    server.throttle = 0.005
    settings = Settings(download_dir=str(tmp_path), connections=4, max_concurrent=2)
    mgr = DownloadManager(settings, Recorder(), store=tmp_path / "tasks.json")
    t1 = mgr.add(Task(url=server.url("/a/file.bin")))
    t2 = mgr.add(Task(url=server.url("/b/file.bin")))
    assert wait_for(lambda: t1.status == Status.COMPLETED and t2.status == Status.COMPLETED, 30)
    assert t1.filename != t2.filename
    got = {(tmp_path / t.filename).read_bytes() for t in (t1, t2)}
    assert got == {a, b}
    mgr.shutdown()


def test_finished_download_counts_as_completed_even_if_paused_at_last_moment(monkeypatch, tmp_path):
    """暫停剛好在檔案改名完成後才送達：run() 已回傳 True，任務必須算完成，不可重新下載出 (1) 副本。"""
    import divebird.engine.manager as m

    class FinishedThenPaused:
        def __init__(self, task, **kw):
            self.task, self.stopped = task, False

        def run(self):
            self.stopped = True          # 暫停在最後一刻送達
            return True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(m, "HttpDownloader", FinishedThenPaused)
    mgr = DownloadManager(Settings(download_dir=str(tmp_path)), Recorder(), store=tmp_path / "tasks.json")
    t = mgr.add(Task(url="http://127.0.0.1:1/x.bin", filename="x.bin"))
    assert wait_for(lambda: t.status == Status.COMPLETED, 5)
    mgr.shutdown()


def test_redownload_refused_while_merging(tmp_path):
    mgr = DownloadManager(Settings(download_dir=str(tmp_path)), Recorder(), store=tmp_path / "tasks.json")
    t = Task(url="https://example.com/v.m3u8", status=Status.PROCESSING)
    mgr.tasks[t.id] = t
    assert mgr.redownload(t.id) is False
    assert t.status == Status.PROCESSING
    mgr.shutdown()


def test_second_server_cannot_share_port():
    """Windows 上不可讓第二個程式綁上已被佔用的埠（否則會出現「已啟用」的假象）。"""
    port = _free_port()
    first = ApiServer(port, on_download=lambda d: None, on_show=lambda: None)
    assert first.start()
    try:
        second = ApiServer(port, on_download=lambda d: None, on_show=lambda: None)
        assert second.start() is False
    finally:
        first.stop()


def test_running_legacy_app_is_detected(monkeypatch, tmp_path):
    """舊版 OpenDM 仍在執行時要偵測得到（才能在搬移資料前請使用者先結束它）。"""
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from divebird.gui import app as gui_app

    class Legacy(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = _json.dumps({"ok": True, "app": "OpenDM"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Legacy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if sys.platform == "darwin":
        legacy_dir = tmp_path / "Library" / "Application Support" / "OpenDM"
    else:
        legacy_dir = tmp_path / ("OpenDM" if os.name == "nt" else "opendm")
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "settings.json").write_text(_json.dumps({"port": httpd.server_address[1]}), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))     # macOS：~/Library/Application Support
    try:
        assert gui_app._running_legacy_app() == "OpenDM"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_startup_checks_do_not_wait_when_nothing_runs():
    """沒有其他執行個體時，啟動檢查要立刻結束。
    Windows 連到沒人監聽的本機埠要重試約 2 秒才失敗，以前每次啟動都白等 2.5 秒。"""
    from divebird.gui import app as gui_app

    port = _free_port()
    start = time.perf_counter()
    assert gui_app._listening(port) is False
    assert gui_app._forward_to_running(port, []) is False
    assert time.perf_counter() - start < 0.3

    server = ApiServer(port, on_download=lambda d: None, on_show=lambda: None)
    assert server.start()
    try:
        assert gui_app._listening(port) is True
    finally:
        server.stop()
    assert gui_app._listening(port) is False  # 結束後立刻判斷為沒人在用（殘留的 TIME_WAIT 不算）
