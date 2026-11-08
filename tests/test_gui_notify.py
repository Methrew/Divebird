"""點系統匣通知：打開 Divebird，並選取通知裡的那筆下載。"""
import os
import socket

from divebird.config import Settings
from divebird.models import Status, Task


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_clicking_a_notification_opens_divebird(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "appdata"))     # macOS：~/Library/Application Support
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    from divebird.gui.app import Controller

    app = QApplication.instance() or QApplication([])
    ctl = Controller(app, Settings(download_dir=str(tmp_path / "dl"), port=_free_port()))
    try:
        done = Task(url="https://example.com/a.zip", filename="a.zip", status=Status.COMPLETED)
        paused = Task(url="https://example.com/b.zip", filename="b.zip", status=Status.PAUSED)
        for t in (paused, done):
            ctl.manager.tasks[t.id] = t
            ctl.bridge.task_added(t)
        w = ctl.window
        w.hide()
        w.cats.setCurrentRow(2)          # 「未完成」：已完成的那筆被篩掉
        shown = []
        monkeypatch.setattr(ctl.tray, "showMessage", lambda title, msg, *a: shown.append((title, msg)))

        ctl._on_finished(done)
        ctl.tray.messageClicked.emit()
        app.processEvents()
        assert shown == [("下載完成", "a.zip")]
        assert w.isVisible() and w.proxy.category == "all"
        assert [t.id for t in w.selected_tasks()] == [done.id]

        # 不是某筆下載的通知（例如 AI 新增下載的提示）：只打開視窗，不改變選取
        w.hide()
        ctl.notify("AI 已新增下載", "c.zip")
        ctl.tray.messageClicked.emit()
        app.processEvents()
        assert w.isVisible() and [t.id for t in w.selected_tasks()] == [done.id]
    finally:
        ctl.manager.tasks.clear()
        ctl.manager.shutdown()
        if ctl.server:
            ctl.server.stop()
        if ctl.tray:
            ctl.tray.hide()
        ctl.window.deleteLater()
        app.processEvents()
