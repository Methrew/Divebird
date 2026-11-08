"""下載佇列管理：排程、暫停 / 續傳、持久化。與 GUI 無關，可單獨使用或測試。"""
from __future__ import annotations

import errno
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

import requests

from ..config import Settings, data_dir
from ..macos import mark_quarantine
from ..models import Kind, Status, Task
from ..utils import sanitize_filename
from .http_engine import DownloadError, HttpDownloader, ManifestDetected, RateLimiter
from .media_engine import MediaDownloader


class Listener(Protocol):
    def task_added(self, task: Task) -> None: ...
    def task_removed(self, task_id: str) -> None: ...
    def task_changed(self, task: Task) -> None: ...
    def task_finished(self, task: Task) -> None: ...


class _NullListener:
    def task_added(self, task): pass
    def task_removed(self, task_id): pass
    def task_changed(self, task): pass
    def task_finished(self, task): pass


# macOS 拒絕存取「下載項目」等受保護的資料夾時（使用者在系統詢問時按了「不允許」），錯誤是 EPERM
MAC_FOLDER_HINT = ("（macOS 不允許 Divebird 存取這個資料夾：請到「系統設定 → 隱私權與安全性 → 檔案與資料夾」"
                   "允許 Divebird，或在設定中改用其他下載資料夾）")


def friendly_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        hint = {401: "需要登入", 403: "拒絕存取（連結可能已過期）", 404: "找不到檔案", 410: "檔案已移除",
                429: "請求過於頻繁", 500: "伺服器錯誤", 503: "伺服器忙碌"}.get(code, "")
        return f"HTTP {code} {hint}".strip()
    if isinstance(e, requests.ConnectionError):
        return "無法連線到伺服器"
    if isinstance(e, requests.Timeout):
        return "連線逾時"
    if isinstance(e, DownloadError):
        msg = str(e)        # 影音引擎的錯誤是 yt-dlp 的訊息，存取被拒時含「[Errno 1]」
        return msg + MAC_FOLDER_HINT if sys.platform == "darwin" and "[Errno 1]" in msg else msg
    if isinstance(e, OSError):
        msg = f"檔案錯誤：{e.strerror or e}"
        return msg + MAC_FOLDER_HINT if sys.platform == "darwin" and e.errno == errno.EPERM else msg
    return f"{e.__class__.__name__}: {e}"


class DownloadManager:
    def __init__(self, settings: Settings, listener: Listener | None = None, store: Path | None = None):
        self.settings = settings
        self.listener: Listener = listener or _NullListener()
        self.tasks: dict[str, Task] = {}
        self._runners: dict[str, object] = {}
        self._lock = threading.RLock()
        self.limiter = RateLimiter(settings.speed_limit_kbps * 1024)
        self._store = store or data_dir() / "tasks.json"
        self._dirty = threading.Event()
        self._closing = threading.Event()
        self._save_lock = threading.Lock()
        self._load()
        threading.Thread(target=self._autosave_loop, daemon=True, name="autosave").start()

    # ---------------------------------------------------------------- 持久化
    def _load(self) -> None:
        try:
            raw = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for d in raw:
            try:
                t = Task.from_dict(d)
            except TypeError:
                continue
            if t.status in (Status.DOWNLOADING, Status.PROCESSING, Status.QUEUED):
                t.status = Status.PAUSED     # 上次未完成的任務，等使用者手動繼續
            self.tasks[t.id] = t

    def save(self) -> None:
        with self._save_lock:
            with self._lock:
                data = [t.to_dict() for t in self.tasks.values()]
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._store)

    def _autosave_loop(self) -> None:
        while not self._closing.is_set():
            if self._dirty.wait(3):
                self._dirty.clear()
                try:
                    self.save()
                except OSError:
                    pass
                time.sleep(3)

    def _changed(self, task: Task, persist: bool = False) -> None:
        if persist:
            self._dirty.set()
        self.listener.task_changed(task)

    # ---------------------------------------------------------------- 檔名分配
    def _reserve(self, task: Task, path: Path) -> Path:
        """回傳不會和磁碟上的檔案、也不會和其他未完成任務撞名的路徑，並立即登記給 task。"""
        folder = os.path.normcase(os.path.abspath(path.parent))
        with self._lock:
            taken = {
                t.filename.lower() for t in self.tasks.values()
                if t is not task and t.filename and t.status != Status.COMPLETED
                and os.path.normcase(os.path.abspath(t.save_dir)) == folder
            }

            def busy(c: Path) -> bool:
                return (c.name.lower() in taken or c.exists()
                        or c.with_name(c.name + ".part").exists())

            candidate, i = path, 1
            while busy(candidate):
                candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
                i += 1
            task.filename = candidate.name
            return candidate

    # ---------------------------------------------------------------- 操作
    def apply_settings(self) -> None:
        self.limiter.set_rate(self.settings.speed_limit_kbps * 1024)
        self.schedule()

    def add(self, task: Task, start: bool = True) -> Task:
        task.save_dir = task.save_dir or self.settings.download_dir
        if task.filename:
            task.filename = sanitize_filename(task.filename)
            # 已有同名檔案、暫存檔或其他任務在用同一個名字時自動改名，避免互相覆蓋或被 yt-dlp 誤判為已下載
            self._reserve(task, Path(task.save_dir) / task.filename)
        task.status = Status.QUEUED if start else Status.PAUSED
        with self._lock:
            self.tasks[task.id] = task
        self.listener.task_added(task)
        self._dirty.set()
        self.schedule()
        return task

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def start(self, task_id: str) -> None:
        with self._lock:
            t = self.tasks.get(task_id)
            if t and t.status in (Status.PAUSED, Status.ERROR):
                t.status = Status.QUEUED
                t.error = ""
                self._changed(t, persist=True)
        self.schedule()

    def pause(self, task_id: str) -> None:
        with self._lock:
            t = self.tasks.get(task_id)
            if not t or t.status not in (Status.QUEUED, Status.DOWNLOADING, Status.PROCESSING):
                return
            if t.status == Status.PROCESSING:
                return      # 合併中不可中斷，避免產生壞檔
            runner = self._runners.get(task_id)
            t.status = Status.PAUSED
            t.speed = 0.0
            self._changed(t, persist=True)
        if runner:
            runner.stop()

    def start_all(self) -> None:
        for tid in list(self.tasks):
            self.start(tid)

    def pause_all(self) -> None:
        for tid in list(self.tasks):
            self.pause(tid)

    def redownload(self, task_id: str) -> bool:
        """重新下載。合併影音中的任務不可重新下載（回傳 False）。等待與清理在背景進行，不卡住 GUI。"""
        t = self.tasks.get(task_id)
        if not t or t.status == Status.PROCESSING:
            return False
        was_completed = t.status == Status.COMPLETED
        self.pause(task_id)

        def work():
            self._wait_runner(task_id, timeout=120)
            if task_id in self._runners or task_id not in self.tasks:
                return
            self._delete_temp_files(t)
            if was_completed and t.filename:
                self._reserve(t, t.filepath)    # 原檔還在：改用新檔名，不覆蓋
            t.downloaded, t.total, t.error, t.finished_at = 0, 0, "", 0.0
            t.status = Status.QUEUED
            self._changed(t, persist=True)
            self.schedule()

        threading.Thread(target=work, daemon=True).start()
        return True

    def remove(self, task_id: str, delete_files: bool = False) -> None:
        t = self.tasks.get(task_id)
        if not t:
            return
        self.pause(task_id)
        with self._lock:
            self.tasks.pop(task_id, None)
        self.listener.task_removed(task_id)
        self._dirty.set()

        def cleanup():
            self._wait_runner(task_id, timeout=600)
            if delete_files:
                self._delete_temp_files(t)
                if t.status == Status.COMPLETED and t.filename:
                    try:
                        t.filepath.unlink(missing_ok=True)
                    except OSError:
                        pass
            elif t.status != Status.COMPLETED:
                self._delete_temp_files(t)

        threading.Thread(target=cleanup, daemon=True).start()

    def _wait_runner(self, task_id: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while task_id in self._runners and time.monotonic() < deadline:
            time.sleep(0.05)

    @staticmethod
    def _delete_temp_files(t: Task) -> None:
        if not t.filename:
            return
        base = Path(t.save_dir)
        paths = [base / (t.filename + ".part"), base / (t.filename + ".part.json")]
        if t.kind == Kind.MEDIA:
            stem = os.path.splitext(t.filename)[0]
            try:
                for p in base.iterdir():
                    n = p.name
                    if n.startswith(stem + ".") and (n.endswith((".part", ".ytdl")) or ".part-Frag" in n
                                                     or ".temp." in n):
                        paths.append(p)
            except OSError:
                pass
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------------- 排程
    def schedule(self) -> None:
        with self._lock:
            if self._closing.is_set():
                return
            slots = max(1, self.settings.max_concurrent) - len(self._runners)
            for t in list(self.tasks.values()):
                if slots <= 0:
                    break
                if t.status == Status.QUEUED and t.id not in self._runners:
                    self._launch(t)
                    slots -= 1

    def _launch(self, t: Task) -> None:
        t.status = Status.DOWNLOADING
        t.error = ""
        on_update = lambda task: self.listener.task_changed(task)  # noqa: E731
        if t.kind == Kind.MEDIA:
            runner = MediaDownloader(t, self.settings, rate_limit=self.limiter.rate, on_update=on_update)
        else:
            runner = HttpDownloader(t, connections=self.settings.connections,
                                    max_retries=self.settings.max_retries,
                                    limiter=self.limiter, on_update=on_update,
                                    name_reserver=lambda path, task=t: self._reserve(task, path))
        self._runners[t.id] = runner
        self._changed(t, persist=True)
        threading.Thread(target=self._run, args=(t, runner), daemon=True, name=f"task-{t.id}").start()

    def _run(self, t: Task, runner) -> None:
        finished = False
        try:
            ok = runner.run()
            if ok:
                # macOS：跟瀏覽器下載的檔案一樣加上隔離屬性，開啟時交給 Gatekeeper 檢查
                # （在標示完成之前加上，使用者才不會搶先開啟還沒有隔離屬性的檔案）
                if t.filename:
                    mark_quarantine(t.filepath, t.url)
                t.status = Status.COMPLETED
                t.finished_at = time.time()
                t.cookies, t.headers = [], {}     # 完成後不再保留登入資訊
                finished = True
            elif t.status in Status.ACTIVE:
                t.status = Status.PAUSED
        except ManifestDetected:
            # 網址其實是 HLS / DASH 播放清單（例如沒有副檔名的 /master?id=1）：改用影音引擎重新排入佇列，
            # 否則只會存下一份文字播放清單
            if t.status in Status.ACTIVE:
                t.kind = Kind.MEDIA
                if not t.filename and t.title:
                    t.filename = sanitize_filename(t.title) + "." + (self.settings.merge_format or "mp4")
                t.status = Status.QUEUED
        except Exception as e:  # noqa: BLE001
            if runner.stopped:
                if t.status in Status.ACTIVE:
                    t.status = Status.PAUSED
            else:
                t.status = Status.ERROR
                t.error = friendly_error(e)
        finally:
            t.speed = 0.0
            t.active_connections = 0
            with self._lock:
                if self._runners.get(t.id) is runner:
                    del self._runners[t.id]
            self._changed(t, persist=True)
            if finished:
                self.listener.task_finished(t)
            self.schedule()

    def shutdown(self, timeout: float = 8) -> None:
        self._closing.set()
        with self._lock:
            runners = list(self._runners.items())
        for tid, r in runners:
            r.stop()
        deadline = time.monotonic() + timeout
        while self._runners and time.monotonic() < deadline:
            time.sleep(0.05)
        for t in self.tasks.values():
            if t.status in Status.ACTIVE or t.status == Status.QUEUED:
                t.status = Status.PAUSED
        try:
            self.save()
        except OSError:
            pass
