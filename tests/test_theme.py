"""系統在執行中切換淺色／深色外觀（例如 macOS 的「自動」外觀在日落時切換）時，Divebird 要跟著重新著色。"""
import os
import socket

from divebird.config import Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_switching_appearance_recolors(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "appdata"))     # macOS：~/Library/Application Support
    from PySide6.QtWidgets import QApplication

    from divebird.gui import app as gui_app

    app = QApplication.instance() or QApplication([])
    old_sheet = app.styleSheet()
    ctl = gui_app.Controller(app, Settings(download_dir=str(tmp_path / "dl"), port=_free_port()))
    try:
        w = ctl.window
        start = ctl.dark
        toolbar_icon, category_icon = w.act_add.icon().cacheKey(), w.cats.item(0).icon().cacheKey()
        monkeypatch.setattr(gui_app, "is_dark", lambda: not start)
        ctl._color_scheme_changed()             # 等這一輪事件處理完（調色盤換好）才重新著色
        for _ in range(50):
            if ctl.dark is not start:
                break
            app.processEvents()
        assert ctl.dark is w.dark is w.model.dark is w.delegate.dark is (not start)
        assert app.styleSheet() == gui_app.stylesheet(not start)
        assert w.act_add.icon().cacheKey() != toolbar_icon and w.cats.item(0).icon().cacheKey() != category_icon
        sheet = app.styleSheet()
        ctl._apply_color_scheme()               # 外觀沒有改變：什麼都不做
        assert app.styleSheet() == sheet
    finally:
        app.setStyleSheet(old_sheet)
        ctl.manager.shutdown()
        if ctl.server:
            ctl.server.stop()
        if ctl.tray:
            ctl.tray.hide()
        ctl.window.deleteLater()
        app.processEvents()
        app.processEvents()
