"""MCP 伺服器：兩個協定世代、傳輸層安全、工具與權限。

用暫存的設定資料夾與隨機埠啟動 ApiServer，不碰使用者正在執行的 Divebird（17890）。
"""
import base64
import hashlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import requests

from divebird.config import Settings
from divebird.engine.manager import DownloadManager
from divebird.mcp import token as mcp_token
from divebird.mcp import tools as mcp_tools
from divebird.mcp.service import McpService
from divebird.models import Status, Task
from divebird.netpolicy import BlockedAddress, block_private_redirects, check_url
from divebird.server import ApiServer

MODERN = "2026-07-28"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeBackend:
    """代替 GUI：記錄確認視窗與通知，可模擬使用者按「開始下載」或「取消」。"""

    def __init__(self, settings: Settings, store):
        self.settings = settings
        self.manager = DownloadManager(settings, store=store)
        self.confirmations: list[tuple[Task, bool, str]] = []
        self.announcements: list[tuple[str, str]] = []
        self.service: McpService | None = None

    def confirm(self, task, start, client):
        self.confirmations.append((task, start, client))

    def announce(self, title, message):
        self.announcements.append((title, message))

    def approve(self, task):
        self.manager.add(task, True)
        self.service.tools.confirmation_done(task.id, True)

    def reject(self, task):
        self.service.tools.confirmation_done(task.id, False)


class Mcp:
    def __init__(self, port: int, token: str):
        self.port, self.token, self.ids = port, token, 0

    def post(self, body, headers=None, raw=False):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Host": f"127.0.0.1:{self.port}", "Authorization": f"Bearer {self.token}"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = body if raw else json.dumps(body).encode()
        return requests.post(f"http://127.0.0.1:{self.port}/mcp", data=data, headers=h, timeout=40)

    def legacy(self, method, params=None, version="2025-11-25"):
        self.ids += 1
        r = self.post({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}},
                      {"MCP-Protocol-Version": version})
        return r

    def modern(self, method, params=None, headers=None, meta=None):
        self.ids += 1
        params = dict(params or {})
        params["_meta"] = meta if meta is not None else {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientInfo": {"name": "pytest-client", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        h = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
        if method == "tools/call":
            h["Mcp-Name"] = params.get("name")
        h.update(headers or {})
        return self.post({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params}, h)

    def call(self, name, arguments=None):
        r = self.modern("tools/call", {"name": name, "arguments": arguments or {}})
        assert r.status_code == 200, r.text
        return r.json()["result"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "appdata"))     # macOS：~/Library/Application Support
    settings = Settings(download_dir=str(tmp_path / "dl"), port=_free_port(), mcp_enabled=True,
                        mcp_confirm="never", mcp_allow_private=True)    # 測試伺服器在 127.0.0.1
    backend = FakeBackend(settings, store=tmp_path / "tasks.json")
    service = McpService(settings, backend)
    backend.service = service
    server = ApiServer(settings.port, on_show=lambda: None, mcp=service,
                       on_download=lambda p: p.get("mcp_request") and service.tools.fulfill_browser_request(p))
    assert server.start()
    client = Mcp(settings.port, mcp_token.ensure())
    yield settings, backend, client
    server.stop()
    backend.manager.shutdown()


def _wait_status(client, task_id, statuses, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.call("get_download", {"task_id": task_id, "wait_seconds": 2})["structuredContent"]
        if data["status"] in statuses:
            return data
    raise AssertionError(f"逾時：{task_id} 沒有進入 {statuses}")


# ---------------------------------------------------------------------- 協定
def test_legacy_handshake_and_tools(env):
    _, _, c = env
    r = c.legacy("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "old", "version": "1"}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"]["tools"] == {"listChanged": False}
    assert res["serverInfo"]["name"] == "divebird" and "instructions" in res
    assert "Mcp-Session-Id" not in r.headers, "不核發 session：每個請求都能獨立處理"
    newer = c.legacy("initialize", {"protocolVersion": "2099-01-01", "capabilities": {}}).json()["result"]
    assert newer["protocolVersion"] == "2025-11-25"

    note = c.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"MCP-Protocol-Version": "2025-06-18"})
    assert note.status_code == 202 and note.content == b""
    assert c.legacy("ping").json()["result"] == {}
    names = [t["name"] for t in c.legacy("tools/list").json()["result"]["tools"]]
    assert names == ["get_status", "probe_url", "download", "get_download", "list_downloads",
                     "control_download", "remove_download", "list_browser_media", "download_browser_media"]
    call = c.legacy("tools/call", {"name": "get_status", "arguments": {}}).json()["result"]
    assert call["isError"] is False and call["structuredContent"]["app"] == "Divebird"
    assert "resultType" not in call, "舊世代的結果不帶 resultType"
    # 2025-03-26 的用戶端不帶 MCP-Protocol-Version 標頭
    r = c.post({"jsonrpc": "2.0", "id": 99, "method": "ping"}, {"MCP-Protocol-Version": None})
    assert r.json()["result"] == {}


def test_modern_discover_list_and_call(env):
    _, _, c = env
    res = c.modern("server/discover").json()["result"]
    assert res["resultType"] == "complete"
    assert res["supportedVersions"] == [MODERN]
    assert res["capabilities"]["tools"] == {"listChanged": False}
    assert res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "divebird"
    assert res["ttlMs"] > 0 and res["cacheScope"] == "public"
    tools = c.modern("tools/list").json()["result"]
    assert tools["resultType"] == "complete" and tools["ttlMs"] > 0 and len(tools["tools"]) == 9
    for t in tools["tools"]:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    call = c.call("get_status")
    assert call["resultType"] == "complete" and call["structuredContent"]["version"]
    assert json.loads(call["content"][0]["text"]) == call["structuredContent"], "文字內容是同一份 JSON（相容舊用戶端）"


def test_modern_validation_errors(env):
    _, _, c = env
    r = c.modern("tools/list", headers={"Mcp-Method": None})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    r = c.modern("tools/list", headers={"MCP-Protocol-Version": "2025-11-25"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    r = c.modern("tools/call", {"name": "get_status", "arguments": {}}, headers={"Mcp-Name": "download"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    encoded = "=?base64?" + base64.b64encode(b"get_status").decode() + "?="
    r = c.modern("tools/call", {"name": "get_status", "arguments": {}}, headers={"Mcp-Name": encoded})
    assert r.status_code == 200, "Mcp-Name 可用 base64 哨兵格式"

    meta = {"io.modelcontextprotocol/protocolVersion": "2099-01-01", "io.modelcontextprotocol/clientCapabilities": {}}
    r = c.modern("tools/list", headers={"MCP-Protocol-Version": "2099-01-01"}, meta=meta)
    err = r.json()["error"]
    assert r.status_code == 400 and err["code"] == -32022
    assert MODERN in err["data"]["supported"] and err["data"]["requested"] == "2099-01-01"

    r = c.modern("ping")                      # 2026-07-28 已移除 ping
    assert r.status_code == 404 and r.json()["error"]["code"] == -32601
    r = c.modern("tools/list", meta={"io.modelcontextprotocol/protocolVersion": MODERN})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32602, "缺少 clientCapabilities"
    r = c.modern("tools/call", {"name": "no_such_tool", "arguments": {}})
    assert r.json()["error"]["code"] == -32602
    r = c.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert r.status_code == 400, "不支援 JSON-RPC 批次"
    r = c.post(b"{not json", raw=True)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_transport_security(env):
    settings, _, c = env
    base = f"http://127.0.0.1:{settings.port}/mcp"
    r = requests.get(base, timeout=5)
    assert r.status_code == 405 and r.headers.get("Allow") == "POST", "GET 不需要權杖也回 405"
    assert requests.delete(base, timeout=5).status_code == 405
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert c.post(body, {"Origin": "https://evil.example"}).status_code == 403, "網頁發出的請求一律拒絕"
    assert c.post(body, {"Origin": "chrome-extension://abc"}).status_code == 403
    assert c.post(body, {"Host": "evil.example"}).status_code == 403, "DNS rebinding"
    r = c.post(body, {"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and "Bearer" in r.headers.get("WWW-Authenticate", "")
    assert c.post(body, {"Authorization": None}).status_code == 401
    settings.mcp_enabled = False
    r = c.post(body)
    assert r.status_code == 503 and "AI 整合" in r.json()["error"]["message"]


def test_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))     # macOS：~/Library/Application Support
    first = mcp_token.ensure()
    assert len(first) >= 32 and mcp_token.ensure() == first
    second = mcp_token.reset()
    assert second != first and mcp_token.load() == second
    assert mcp_token.matches(f"Bearer {second}", second) and mcp_token.matches(f"bearer  {second} ", second)
    assert not mcp_token.matches(f"Bearer {first}", second) and not mcp_token.matches(None, second)
    assert not mcp_token.matches("Bearer x", "")


# ---------------------------------------------------------------------- 工具
def test_download_without_confirmation(env, server):
    settings, backend, c = env
    data = server.add("/files/report.bin", 700_000)
    res = c.call("download", {"url": server.url("/files/report.bin")})
    assert res["isError"] is False, res
    started = res["structuredContent"]
    assert started["status"] in (Status.QUEUED, Status.DOWNLOADING, Status.COMPLETED)
    done = _wait_status(c, started["task_id"], (Status.COMPLETED, Status.ERROR))
    assert done["status"] == Status.COMPLETED, done
    assert done["progress_percent"] == 100.0 and done["source"] == "ai"
    with open(done["path"], "rb") as f:
        assert hashlib.sha256(f.read()).digest() == hashlib.sha256(data).digest()
    assert backend.announcements, "沒有確認視窗時要顯示通知"
    listed = c.call("list_downloads", {"source": "mcp"})["structuredContent"]
    assert listed["total"] == 1 and listed["items"][0]["task_id"] == started["task_id"]
    assert "cookies" not in json.dumps(listed) and "headers" not in json.dumps(listed)


def test_download_waits_for_confirmation(env, server):
    settings, backend, c = env
    settings.mcp_confirm = "always"
    server.add("/a.bin", 300_000)
    res = c.call("download", {"url": server.url("/a.bin"), "filename": "第一集.bin"})["structuredContent"]
    assert res["status"] == "awaiting_confirmation"
    task, start, client = backend.confirmations[-1]
    assert task.id == res["task_id"] and start is True and client == "pytest-client"
    assert task.source == "mcp:pytest-client"
    waiting = c.call("get_download", {"task_id": task.id, "wait_seconds": 1})["structuredContent"]
    assert waiting["status"] == "awaiting_confirmation"
    paused = c.call("control_download", {"task_id": task.id, "action": "pause"})
    assert paused["isError"] and "確認" in paused["content"][0]["text"]
    backend.approve(task)
    done = _wait_status(c, task.id, (Status.COMPLETED, Status.ERROR))
    assert done["status"] == Status.COMPLETED and done["filename"] == "第一集.bin"

    other = c.call("download", {"url": server.url("/a.bin")})["structuredContent"]
    backend.reject(backend.confirmations[-1][0])
    rejected = c.call("get_download", {"task_id": other["task_id"]})["structuredContent"]
    assert rejected["status"] == "rejected"
    assert c.call("remove_download", {"task_id": other["task_id"]})["structuredContent"]["removed"]
    assert c.call("get_download", {"task_id": other["task_id"]})["isError"]


def test_pending_confirmations_are_capped(env, server):
    settings, _, c = env
    settings.mcp_confirm = "always"
    server.add("/x.bin", 1000)
    for _ in range(mcp_tools.MAX_PENDING):
        assert c.call("download", {"url": server.url("/x.bin")})["isError"] is False
    res = c.call("download", {"url": server.url("/x.bin")})
    assert res["isError"] and "等使用者" in res["content"][0]["text"]


def test_private_network_is_blocked_by_default(env, server):
    settings, _, c = env
    settings.mcp_allow_private = False
    server.add("/a.bin", 1000)
    for tool in ("download", "probe_url"):
        res = c.call(tool, {"url": server.url("/a.bin")})
        assert res["isError"] and "內網" in res["content"][0]["text"]
        assert "設定 → AI 整合" in res["content"][0]["text"]       # 讓模型能告訴使用者去哪裡開放
    res = c.call("download", {"url": "http://localhost:1/x"})
    assert res["isError"]


def test_redirect_to_private_address_is_refused():
    r = requests.Response()
    r.status_code = 302
    r.url = "http://93.184.216.34/start"
    r.headers["location"] = "http://127.0.0.1:8080/admin"
    with pytest.raises(BlockedAddress):
        block_private_redirects(r)
    r.headers["location"] = "http://8.8.8.8/fine"
    assert block_private_redirects(r) is r
    with pytest.raises(BlockedAddress):
        check_url("http://[::ffff:192.168.0.1]/")       # IPv4 對應的 IPv6 也要擋
    check_url("http://1.1.1.1/")


def test_subdir_permission_and_traversal(env, server):
    settings, _, c = env
    server.add("/a.bin", 1000)
    url = server.url("/a.bin")
    for bad in ("../outside", "a/../../b", "/etc", "C:/Windows", "C:temp", "\\\\server\\share"):
        res = c.call("download", {"url": url, "subdir": bad})
        assert res["isError"], bad
    ok = c.call("download", {"url": url, "subdir": "課程/第一週"})["structuredContent"]
    assert ok["save_dir"] == os.path.join(os.path.realpath(settings.download_dir), "課程", "第一週")
    settings.mcp_allow_subdir = False
    res = c.call("download", {"url": url, "subdir": "x"})
    assert res["isError"] and "子資料夾" in res["content"][0]["text"]


def test_credentials_need_permission(env, server):
    settings, _, c = env
    server.add("/a.bin", 1000)
    url = server.url("/a.bin")
    res = c.call("download", {"url": url, "cookies": [{"name": "sid", "value": "1"}]})
    assert res["isError"] and "Cookie" in res["content"][0]["text"]
    res = c.call("download", {"url": url, "headers": {"Authorization": "Bearer x"}})
    assert res["isError"]
    assert c.call("download", {"url": url, "headers": {"X-Requested-With": "x"}})["isError"] is False
    settings.mcp_allow_cookies = True
    res = c.call("download", {"url": url, "cookies": [{"name": "sid", "value": "1"}],
                              "headers": {"Authorization": "Bearer x"}})
    assert res["isError"] is False


def test_remove_and_delete_permission(env, server):
    settings, _, c = env
    server.add("/a.bin", 2000)
    tid = c.call("download", {"url": server.url("/a.bin")})["structuredContent"]["task_id"]
    path = _wait_status(c, tid, (Status.COMPLETED,))["path"]
    res = c.call("remove_download", {"task_id": tid, "delete_file": True})
    assert res["isError"] and "刪除" in res["content"][0]["text"]
    settings.mcp_allow_delete = True
    out = c.call("remove_download", {"task_id": tid, "delete_file": True})["structuredContent"]
    assert out == {"task_id": tid, "removed": True, "file_deleted": True}
    deadline = time.monotonic() + 10
    while os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(path)


def test_pause_and_resume(env, server):
    settings, _, c = env
    server.throttle = 0.2                     # 每條連線約 320 KB/s：8 MB 要好幾秒
    server.add("/slow.bin", 8 * 1024 * 1024)
    tid = c.call("download", {"url": server.url("/slow.bin")})["structuredContent"]["task_id"]
    time.sleep(0.5)
    paused = c.call("control_download", {"task_id": tid, "action": "pause"})["structuredContent"]
    assert paused["status"] == Status.PAUSED
    server.throttle = 0
    c.call("control_download", {"task_id": tid, "action": "resume"})
    assert _wait_status(c, tid, (Status.COMPLETED,))["status"] == Status.COMPLETED


def test_probe_url(env, server):
    _, _, c = env
    server.add("/files/data.zip", 12345)
    res = c.call("probe_url", {"url": server.url("/files/data.zip")})["structuredContent"]
    assert res["kind"] == "file" and res["size_bytes"] == 12345 and res["resumable"] is True
    server.files["/page"] = b"<html><head><title>x</title></head><body>no video here</body></html>"
    server.content_types["/page"] = "text/html; charset=utf-8"
    page = c.call("probe_url", {"url": server.url("/page")})["structuredContent"]
    assert page["kind"] == "page"


def test_quality_values(env, server):
    """畫質：任何高度（144p、240p……）、預設值與 probe_url 的格式字串都接受；其他字串立即拒絕，而不是開始下載後才失敗。"""
    _, backend, c = env
    quality = backend.service.tools._quality
    assert quality({"quality": "144p"}) == "bv*[height<=144]+ba/b[height<=144]/bv*+ba/b"
    assert quality({"quality": "720P60"}) == quality({"quality": "720p"})
    assert quality({"quality": "Best"}) == "bv*+ba/b"
    assert quality({"quality": "audio"}) == "audio:best" and quality({"quality": "audio:mp3"}) == "audio:mp3"
    probe_value = "bv*[height<=240]+ba/b[height<=240]/bv*+ba/b"
    assert quality({"quality": probe_value}) == probe_value and quality({}) == ""
    for bad in ("最低畫質", "lowest", "144p，約 625.74 KB"):
        with pytest.raises(mcp_tools.ToolError):
            quality({"quality": bad})
    server.add("/v.mp4", 1000)
    res = c.call("download", {"url": server.url("/v.mp4"), "quality": "lowest"})
    assert res["isError"] is True and "144p" in res["content"][0]["text"]
    assert not backend.manager.tasks


def test_stdio_bridge(env, tmp_path):
    """stdio 橋接：握手與工具清單在本地回答；工具呼叫轉送到 Divebird 的 /mcp（埠號與權杖從設定資料夾讀取）。"""
    import subprocess
    import sys

    settings, _, _ = env
    settings.save()       # 橋接程式從（暫存的）設定資料夾讀取埠號
    child_env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    proc = subprocess.Popen([sys.executable, "-m", "divebird.mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=child_env)
    meta = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "stdio-test", "version": "1"}}
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "Desk", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "server/discover", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "list_downloads", "arguments": {}, "_meta": meta}},
    ]
    try:
        out, err = proc.communicate("".join(json.dumps(m) + "\n" for m in messages).encode(), timeout=60)
    finally:
        proc.kill()
    replies = {r["id"]: r for r in (json.loads(line) for line in out.decode("utf-8").splitlines())}
    assert sorted(replies) == [1, 2, 3, 4, 5], err.decode("utf-8", "replace")
    assert replies[1]["result"]["serverInfo"]["name"] == "divebird"
    assert len(replies[2]["result"]["tools"]) == 9
    status = replies[3]["result"]
    assert status["isError"] is False and status["structuredContent"]["app"] == "Divebird"
    assert replies[4]["result"]["resultType"] == "complete"
    assert replies[5]["result"]["resultType"] == "complete" and replies[5]["result"]["isError"] is False
    # 記錄寫到 stderr，而且是 UTF-8（AI 應用程式把它存成記錄檔；Windows 預設的代碼頁會變成亂碼）
    assert "橋接已啟動" in err.decode("utf-8")


def test_rate_limit(env, monkeypatch):
    _, _, c = env
    monkeypatch.setattr(mcp_tools, "RATE_LIMIT", 3)
    results = [c.call("get_status") for _ in range(4)]
    assert [r["isError"] for r in results] == [False, False, False, True]


def test_gui_confirmation_dialog(tmp_path, monkeypatch, server):
    """真正的 Qt 介面：AI 發起的下載跳出標示「AI 發起」的確認視窗；按「開始下載」或「取消」都要回報給 AI。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "appdata"))     # macOS：~/Library/Application Support
    from PySide6.QtWidgets import QApplication

    from divebird.gui.app import Controller
    from divebird.mcp.protocol import CallContext

    app = QApplication.instance() or QApplication([])
    settings = Settings(download_dir=str(tmp_path / "dl"), port=_free_port(), mcp_enabled=True,
                        mcp_confirm="always", mcp_allow_private=True, minimize_to_tray=False)
    ctl = Controller(app, settings)
    try:
        server.add("/a.bin", 50_000)
        ctx = CallContext("測試 AI", "2025-11-25")

        def request() -> str:
            res = ctl.mcp.tools.call("download", {"url": server.url("/a.bin")}, ctx)
            assert res["structuredContent"]["status"] == "awaiting_confirmation"
            app.processEvents()
            return res["structuredContent"]["task_id"]

        task_id = request()
        dialog = next(d for d in ctl._dialogs if d.task.id == task_id)
        assert dialog.origin == "測試 AI" and "AI" in dialog.windowTitle()
        assert not dialog.skip_box.isVisibleTo(dialog), "AI 發起的視窗不提供「不再顯示」"
        dialog._submit(True)
        app.processEvents()
        assert ctl.manager.get(task_id) is not None
        deadline = time.monotonic() + 20
        while ctl.manager.get(task_id).status != Status.COMPLETED and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)
        assert ctl.manager.get(task_id).status == Status.COMPLETED

        task_id = request()
        dialog = next(d for d in ctl._dialogs if d.task.id == task_id)
        dialog.reject()
        app.processEvents()
        snap = ctl.mcp.tools.call("get_download", {"task_id": task_id}, ctx)["structuredContent"]
        assert snap["status"] == "rejected"
        assert ctl.manager.get(task_id) is None
    finally:
        ctl.manager.shutdown()
        if ctl.server:
            ctl.server.stop()
        ctl.window.deleteLater()
        app.processEvents()


def test_settings_dialog_ai_tab(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))     # macOS：~/Library/Application Support
    from PySide6.QtWidgets import QApplication

    from divebird.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])
    settings = Settings(download_dir=str(tmp_path / "dl"), port=18123)
    dlg = SettingsDialog(settings)
    assert not dlg._mcp_groups[1].isEnabled(), "未啟用 MCP 時不能修改 AI 權限"
    assert dlg.mcp_url.text() == "http://127.0.0.1:18123/mcp"
    dlg.mcp_enabled.setChecked(True)
    assert dlg._mcp_groups[1].isEnabled() and dlg.mcp_token.text() == mcp_token.load() != ""
    config = json.loads(dlg._mcp_config("json"))["mcpServers"]["divebird"]
    assert config["url"].endswith("/mcp") and config["headers"]["Authorization"] == f"Bearer {mcp_token.load()}"
    assert '[mcp_servers.divebird]' in dlg._mcp_config("codex")
    assert "servers" in json.loads(dlg._mcp_config("vscode"))
    dlg.mcp_private.setChecked(True)
    dlg.mcp_confirm.setCurrentIndex(dlg.mcp_confirm.findData("never"))
    dlg._save()
    assert settings.mcp_enabled and settings.mcp_allow_private and settings.mcp_confirm == "never"
    assert not settings.mcp_allow_delete and settings.mcp_allow_subdir
    dlg.deleteLater()
    app.processEvents()


# ---------------------------------------------------------------------- 瀏覽器偵測到的影音
def _extension(settings, path, payload=None):
    """模擬瀏覽器擴充功能呼叫本機 API（帶擴充功能的 Origin）。"""
    url = f"http://127.0.0.1:{settings.port}{path}"
    headers = {"Origin": "chrome-extension://divebird-test"}
    if payload is None:
        return requests.get(url, headers=headers, timeout=10).json()
    return requests.post(url, json=payload, headers=headers, timeout=10).json()


def test_browser_media_needs_permission(env):
    settings, _, c = env
    assert _extension(settings, "/api/ping")["share_media"] is False
    res = c.call("list_browser_media")
    assert res["isError"] and "偵測到的影音" in res["content"][0]["text"]
    settings.mcp_share_browser_media = True
    assert _extension(settings, "/api/ping")["share_media"] is True
    settings.mcp_enabled = False
    assert _extension(settings, "/api/ping")["share_media"] is False, "MCP 關閉時不分享"


def test_browser_media_flow(env, server):
    """擴充功能回報偵測到的影音 → AI 挑選 → 擴充功能補上 Cookie 送出 → 完成下載。AI 看不到查詢參數與 Cookie。"""
    settings, backend, c = env
    settings.mcp_share_browser_media = True
    data = server.add("/media/lesson.mp4", 500_000)
    secret_url = server.url("/media/lesson.mp4") + "?token=SECRET"
    reply = _extension(settings, "/api/media", {
        "tab_id": 7, "page_url": "https://course.example/lesson/3?session=PRIVATE", "title": "第 3 課",
        "items": [{"url": secret_url, "type": "video", "mime": "video/mp4", "size": 500_000, "time": 1}]})
    assert reply == {"ok": True, "requests": []}

    listed = c.call("list_browser_media")["structuredContent"]
    tab = listed["tabs"][0]
    assert tab["page_title"] == "第 3 課" and tab["page"] == "course.example/lesson/3"
    media = tab["media"][0]
    assert media["name"] == "lesson.mp4" and media["type"] == "video" and media["size_bytes"] == 500_000
    assert "SECRET" not in json.dumps(listed) and "PRIVATE" not in json.dumps(listed)
    assert c.call("list_browser_media", {"query": "第 3 課"})["structuredContent"]["tabs"]
    assert not c.call("list_browser_media", {"query": "nothing"})["structuredContent"]["tabs"]

    started = c.call("download_browser_media", {"media_id": media["media_id"], "filename": "第三課"})
    task_id = started["structuredContent"]["task_id"]
    assert started["structuredContent"]["status"] == "waiting_for_browser"
    waiting = c.call("get_download", {"task_id": task_id})["structuredContent"]
    assert waiting["status"] == "waiting_for_browser" and "SECRET" not in json.dumps(waiting)

    # 擴充功能詢問待處理的下載：拿到完整網址，補上 Cookie 與 Referer 後送出
    pending = _extension(settings, "/api/media/requests")["requests"]
    assert [r["request_id"] for r in pending] == [task_id] and pending[0]["url"] == secret_url
    assert _extension(settings, "/api/download", {
        "url": secret_url, "kind": "http", "mcp_request": task_id, "page_url": "https://course.example/lesson/3",
        "cookies": [{"name": "sid", "value": "browser-cookie", "domain": "127.0.0.1", "path": "/"}],
        "title": "第 3 課"})["ok"]
    done = _wait_status(c, task_id, (Status.COMPLETED, Status.ERROR))
    assert done["status"] == Status.COMPLETED and done["filename"] == "第三課.mp4", "補上網址的副檔名"
    with open(done["path"], "rb") as f:
        assert hashlib.sha256(f.read()).digest() == hashlib.sha256(data).digest()
    task = backend.manager.get(task_id)
    assert task.source == "mcp:pytest-client"
    assert _extension(settings, "/api/media/requests")["requests"] == [], "每個請求只送出一次"

    # 分頁關閉（空清單）後就不再列出
    _extension(settings, "/api/media", {"tab_id": 7, "items": []})
    assert c.call("list_browser_media")["structuredContent"]["tabs"] == []


def test_browser_request_expires(env, server, monkeypatch):
    from divebird.mcp import browser

    settings, _, c = env
    settings.mcp_share_browser_media = True
    _extension(settings, "/api/media", {"tab_id": 1, "page_url": "https://a.example/", "title": "t",
                                        "items": [{"url": server.url("/x.mp4"), "type": "video"}]})
    media_id = c.call("list_browser_media")["structuredContent"]["tabs"][0]["media"][0]["media_id"]
    task_id = c.call("download_browser_media", {"media_id": media_id})["structuredContent"]["task_id"]
    monkeypatch.setattr(browser, "REQUEST_TTL", 0)
    expired = c.call("get_download", {"task_id": task_id})["structuredContent"]
    assert expired["status"] == Status.ERROR and "擴充功能" in expired["error"]
    assert _extension(settings, "/api/media/requests")["requests"] == []
    res = c.call("download_browser_media", {"media_id": "000000000000"})
    assert res["isError"] and "list_browser_media" in res["content"][0]["text"]


def test_browser_media_ignored_when_sharing_is_off(env):
    settings, backend, c = env
    reply = _extension(settings, "/api/media", {"tab_id": 2, "items": [{"url": "https://a.example/v.mp4"}]})
    assert reply == {"ok": True, "requests": []}
    settings.mcp_share_browser_media = True
    assert c.call("list_browser_media")["structuredContent"]["tabs"] == [], "關閉期間送來的清單不保留"

