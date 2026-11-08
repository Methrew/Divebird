"""macOS 的系統整合：資料夾位置、在 Finder 中顯示、MCP 橋接啟動 Divebird.app、隔離屬性、暫存位置。
除了標明只在 macOS 執行的測試，都以假的平台與子程序測試，在任何作業系統都能執行。"""
import errno
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from divebird import config, macos, utils
from divebird.config import Settings
from divebird.mcp import stdio
from divebird.models import Status, Task


def test_data_dir_is_in_application_support(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(config, "IS_MAC", True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)       # Windows 的 Path.home() 不看 HOME
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path / "app")   # 不在可攜模式
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))       # macOS 不使用 XDG 設定
    assert config.data_dir() == tmp_path / "Library" / "Application Support" / "Divebird"
    assert config.default_download_dir() == tmp_path / "Downloads"


def test_open_path_uses_finder(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(utils.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    f = tmp_path / "a.zip"
    f.write_bytes(b"x")
    utils.open_path(f, select=True)              # 在 Finder 中顯示並選取
    utils.open_path(f)                           # 以預設程式開啟
    utils.open_path(tmp_path / "missing.zip", select=True)    # 檔案不在了：開啟所在資料夾
    assert calls == [["open", "-R", str(f)], ["open", str(f)], ["open", str(tmp_path)]]


def test_mcp_bridge_launches_app_bundle(monkeypatch, tmp_path):
    bundle_bin = tmp_path / "Divebird.app" / "Contents" / "MacOS"
    bundle_bin.mkdir(parents=True)
    main_exe = bundle_bin / ("Divebird.exe" if os.name == "nt" else "Divebird")    # launch_command 依 os.name 找主程式
    main_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(bundle_bin / "divebird-mcp"))
    # 經由 LaunchServices 啟動（不是 AI 應用程式的子程序），不搶走前景
    assert stdio.launch_command() == ["/usr/bin/open", "-g", "-a", str(tmp_path / "Divebird.app"),
                                      "--args", "--minimized"]
    main_exe.unlink()
    assert stdio.launch_command() is None


def test_quarantine_origin_keeps_only_host_and_path():
    assert macos.quarantine_origin("https://user:pw@example.com:8443/a/setup.dmg?token=secret#x") == \
        "https://example.com:8443/a/setup.dmg"
    assert macos.quarantine_origin("http://[::1]:8080/f.zip?a=1") == "http://[::1]:8080/f.zip"
    assert macos.quarantine_origin("ftp://example.com/f.zip") == ""
    assert macos.quarantine_origin("http://example.com:bad/f.zip") == ""
    assert macos.quarantine_origin("") == ""


@pytest.mark.skipif(sys.platform != "darwin", reason="只有 macOS 有隔離屬性")
def test_mark_quarantine_sets_attribute(tmp_path):
    f = tmp_path / "setup.dmg"
    f.write_bytes(b"x")
    assert macos.mark_quarantine(f, "https://example.com/setup.dmg?token=secret")
    out = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(f)], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip(), out.stderr


def test_mark_quarantine_does_nothing_elsewhere(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert macos.mark_quarantine(tmp_path / "x.zip", "https://example.com/x.zip") is False


def test_completed_download_is_marked_for_gatekeeper(monkeypatch, server, tmp_path):
    from divebird.engine import manager as manager_module

    marked = []
    monkeypatch.setattr(manager_module, "mark_quarantine", lambda path, url="": marked.append((Path(path), url)))
    server.add("/setup.dmg", 64 * 1024)
    mgr = manager_module.DownloadManager(Settings(download_dir=str(tmp_path / "dl")), store=tmp_path / "tasks.json")
    try:
        t = mgr.add(Task(url=server.url("/setup.dmg")))
        deadline = time.monotonic() + 15
        while t.status != Status.COMPLETED and time.monotonic() < deadline:
            time.sleep(0.05)
        assert t.status == Status.COMPLETED
        # 在標示完成之前就加上了
        assert marked == [(tmp_path / "dl" / "setup.dmg", server.url("/setup.dmg"))]
    finally:
        mgr.shutdown()


def test_permission_denied_on_macos_explains_where_to_allow(monkeypatch):
    from divebird.engine.http_engine import DownloadError
    from divebird.engine.manager import MAC_FOLDER_HINT, friendly_error

    eperm = PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert friendly_error(eperm).endswith(MAC_FOLDER_HINT)
    assert MAC_FOLDER_HINT not in friendly_error(PermissionError(errno.EACCES, "Permission denied"))   # 一般的權限不足
    assert friendly_error(DownloadError("[Errno 1] Operation not permitted: '/Users/me/Downloads/a.mp4'")) \
        .endswith(MAC_FOLDER_HINT)
    monkeypatch.setattr(sys, "platform", "linux")
    assert MAC_FOLDER_HINT not in friendly_error(eperm)


def test_running_from_dmg_or_translocation_is_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Divebird.app" / "Contents" / "MacOS" / "Divebird"))
    flags = {"f_flag": 0}
    monkeypatch.setattr(config.os, "ST_RDONLY", 1, raising=False)                  # Windows 沒有 statvfs
    monkeypatch.setattr(config.os, "statvfs", lambda path: type("R", (), flags)(), raising=False)
    assert not config.running_from_temporary_location()
    flags["f_flag"] = 1                 # 掛載的 .dmg 是唯讀的
    assert config.running_from_temporary_location()
    flags["f_flag"] = 0
    monkeypatch.setattr(sys, "executable",
                        "/private/var/folders/x/T/AppTranslocation/1234/d/Divebird.app/Contents/MacOS/Divebird")
    assert config.running_from_temporary_location()
    monkeypatch.setattr(sys, "frozen", False)
    assert not config.running_from_temporary_location()      # 從原始碼執行


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_extension_buttons_disabled_when_running_from_dmg(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))     # macOS：~/Library/Application Support
    from PySide6.QtWidgets import QApplication, QPushButton

    from divebird.gui import dialogs

    app = QApplication.instance() or QApplication([])  # noqa: F841
    ext = tmp_path / "extension"
    ext.mkdir()
    (ext / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dialogs, "extension_dir", lambda: ext)

    def buttons(temporary: bool) -> dict:
        monkeypatch.setattr(dialogs, "running_from_temporary_location", lambda: temporary)
        dlg = dialogs.SettingsDialog(Settings(download_dir=str(tmp_path / "dl"), port=_free_port()))
        found = {b.text(): b.isEnabled() for b in dlg.findChildren(QPushButton)
                 if b.text() in ("複製路徑", "在 Finder 中顯示", "開啟所在資料夾")}
        dlg.deleteLater()
        return found

    normal, from_dmg = buttons(False), buttons(True)
    assert len(normal) == 2 and all(normal.values())
    assert len(from_dmg) == 2 and not any(from_dmg.values()), "暫存位置的路徑之後會失效，不可提供"


@pytest.fixture
def controller(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "appdata"))     # macOS：~/Library/Application Support
    from PySide6.QtWidgets import QApplication

    from divebird.gui.app import Controller

    app = QApplication.instance() or QApplication([])
    ctl = Controller(app, Settings(download_dir=str(tmp_path / "dl"), port=_free_port()))
    yield ctl
    ctl.manager.shutdown()
    if ctl.server:
        ctl.server.stop()
    if ctl.tray:
        ctl.tray.hide()
    ctl.window.deleteLater()
    app.processEvents()
    app.processEvents()


def test_close_window_shortcut_closes_the_front_window(monkeypatch, controller):
    from PySide6.QtWidgets import QApplication, QDialog

    w = controller.window
    w.show()
    front = QDialog()           # 例如「新增下載」視窗（非模態）
    front.show()
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: front))
    w._close_active_window()
    assert not front.isVisible() and w.isVisible(), "⌘W 要關閉前面的視窗，不是主視窗"
    w.hide()
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: None))
    w._close_active_window()    # 沒有視窗在前面（主視窗縮在選單列）：什麼都不做
    assert not w._tray_hint_shown


def test_mac_menu_has_no_shortcuts_that_act_on_the_main_window(controller):
    from PySide6.QtGui import QKeySequence

    w = controller.window
    if not hasattr(w, "_menubar"):      # 其他平台不會自動建立 macOS 的選單列
        w._build_mac_menu()
    menus = {m.title(): m for m in (a.menu() for a in w._menubar.actions()) if m}
    downloads = [a for a in menus["下載"].actions() if not a.isSeparator()]
    assert all(a.shortcut().isEmpty() for a in downloads), "共用選單列的快捷鍵在對話框裡也會觸發"
    delete = next(a for a in downloads if a.text() == "刪除…")
    assert delete is not w.act_delete
    w.act_delete.setEnabled(True)
    assert delete.isEnabled()
    w.act_delete.setEnabled(False)
    assert not delete.isEnabled()
    close = next(a for a in menus["檔案"].actions() if a.text() == "關閉視窗")
    assert close.shortcut() == QKeySequence(QKeySequence.StandardKey.Close)
