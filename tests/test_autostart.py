"""開機自動啟動與舊版（OpenDM）設定的搬移。Windows 部分用假的 winreg，不會碰到真正的登錄檔。"""
import sys

import pytest

from divebird import autostart

RUN, APPROVED = autostart.RUN_KEY, autostart.APPROVED_KEY
ENABLED, DISABLED = b"\x02" + b"\x00" * 11, b"\x03" + b"\x00" * 11


class _Key:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE, KEY_QUERY_VALUE, REG_SZ, REG_BINARY = 2, 1, 1, 3

    def __init__(self):
        self.keys = {RUN: {}, APPROVED: {}}

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        if path not in self.keys:
            raise FileNotFoundError(path)
        return _Key(self.keys[path])

    def CreateKeyEx(self, root, path, reserved=0, access=0):  # noqa: N802
        return _Key(self.keys.setdefault(path, {}))

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name]

    def SetValueEx(self, key, name, reserved, typ, data):  # noqa: N802
        key.values[name] = (data, typ)

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]


@pytest.fixture
def winreg(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    return fake


def test_windows_legacy_enabled_is_migrated(winreg):
    winreg.keys[RUN]["OpenDM"] = ("old.exe --minimized", 1)
    autostart.migrate_legacy()
    assert "OpenDM" not in winreg.keys[RUN]
    assert "Divebird" in winreg.keys[RUN]
    assert autostart.is_enabled()


def test_windows_legacy_disabled_by_user_stays_off(winreg):
    winreg.keys[RUN]["OpenDM"] = ("old.exe --minimized", 1)
    winreg.keys[APPROVED]["OpenDM"] = (DISABLED, 3)
    autostart.migrate_legacy()
    assert "OpenDM" not in winreg.keys[RUN] and "OpenDM" not in winreg.keys[APPROVED]
    assert "Divebird" not in winreg.keys[RUN], "使用者已停用的自動啟動不可被重新打開"
    assert not autostart.is_enabled()


def test_windows_disabled_in_task_manager_reported_and_reenabled(winreg):
    winreg.keys[RUN]["Divebird"] = ("x.exe", 1)
    winreg.keys[APPROVED]["Divebird"] = (DISABLED, 3)
    assert not autostart.is_enabled(), "設定畫面要反映工作管理員中的停用狀態"
    autostart.set_enabled(True)
    assert autostart.is_enabled()
    assert "Divebird" not in winreg.keys[APPROVED]


def test_windows_no_legacy_does_nothing(winreg):
    autostart.migrate_legacy()
    assert winreg.keys[RUN] == {}


def test_desktop_disabled_parsing():
    assert autostart.desktop_disabled("[Desktop Entry]\nHidden=true\n")
    assert autostart.desktop_disabled("X-GNOME-Autostart-enabled = False")
    assert not autostart.desktop_disabled("[Desktop Entry]\nX-GNOME-Autostart-enabled=true\n")


@pytest.mark.parametrize("legacy_text, expect_new", [
    ("[Desktop Entry]\nExec=opendm --minimized\n", True),
    ("[Desktop Entry]\nExec=opendm --minimized\nHidden=true\n", False),
])
def test_linux_legacy_desktop_migration(monkeypatch, tmp_path, legacy_text, expect_new):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "autostart").mkdir()
    (tmp_path / "autostart" / "opendm.desktop").write_text(legacy_text, encoding="utf-8")
    autostart.migrate_legacy()
    assert not (tmp_path / "autostart" / "opendm.desktop").exists()
    assert (tmp_path / "autostart" / "divebird.desktop").exists() == expect_new


def test_source_checkout_autostarts_through_root_launcher(tmp_path, monkeypatch):
    # 從原始碼執行且已有 Divebird.exe：自動啟動也經過它（先檢查環境、需要時先更新）
    monkeypatch.setattr(sys, "platform", "win32")
    assert autostart.launch_command(tmp_path)[0] != str(tmp_path / "Divebird.exe")
    (tmp_path / "Divebird.exe").write_bytes(b"")
    (tmp_path / "Divebird.bat").write_bytes(b"")
    assert autostart.launch_command(tmp_path) == [str(tmp_path / "Divebird.exe"), "--minimized"]


def test_windows_refresh_updates_command_but_keeps_disabled_state(winreg, monkeypatch):
    monkeypatch.setattr(autostart, "launch_command", lambda: ["C:/new/Divebird.exe", "--minimized"])
    autostart.refresh()
    assert winreg.keys[RUN] == {}, "沒有登記自動啟動時不可自行加上"
    winreg.keys[RUN]["Divebird"] = ("C:/old/divebird-gui.exe -m divebird --minimized", 1)
    winreg.keys[APPROVED]["Divebird"] = (DISABLED, 3)
    autostart.refresh()
    assert winreg.keys[RUN]["Divebird"][0] == "C:/new/Divebird.exe --minimized"
    assert winreg.keys[APPROVED]["Divebird"] == (DISABLED, 3), "使用者在工作管理員的停用設定要保留"
    assert not autostart.is_enabled()


# ---------------------------------------------------------------- macOS（LaunchAgent；launchctl 用假的，不會碰到真正的登入項目）
class FakeLaunchctl:
    def __init__(self):
        self.disabled: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "print-disabled":
            lines = [f'\t\t"{label}" => disabled' for label in sorted(self.disabled)]
            return "\tdisabled services = {\n" + "\n".join(lines) + "\n\t}\n"
        if args[0] == "enable":
            self.disabled.discard(args[1].rsplit("/", 1)[1])
        return ""


@pytest.fixture
def mac(monkeypatch, tmp_path):
    from pathlib import Path

    fake = FakeLaunchctl()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)       # Windows 的 Path.home() 不看 HOME
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(autostart, "_launchctl", fake)
    monkeypatch.setattr(autostart, "launch_command", lambda: ["/Applications/Divebird.app/Contents/MacOS/Divebird",
                                                              "--minimized"])
    return fake


def _agent(tmp_path):
    import plistlib

    path = tmp_path / "Library" / "LaunchAgents" / f"{autostart.MAC_BUNDLE_ID}.plist"
    return path, (plistlib.loads(path.read_bytes()) if path.exists() else None)


def test_macos_launch_agent_roundtrip(mac, tmp_path):
    assert not autostart.is_enabled()
    autostart.set_enabled(True)
    path, data = _agent(tmp_path)
    assert data["Label"] == autostart.MAC_BUNDLE_ID and data["RunAtLoad"] is True
    assert data["ProgramArguments"] == ["/Applications/Divebird.app/Contents/MacOS/Divebird", "--minimized"]
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert not path.exists() and not autostart.is_enabled()
    assert not any(c[0] == "bootout" for c in mac.calls), "不可 bootout：登入時啟動的 Divebird 就是這個工作"


def test_macos_disabled_in_login_items_reported_and_reenabled(mac, tmp_path):
    autostart.set_enabled(True)
    mac.disabled.add(autostart.MAC_BUNDLE_ID)        # 使用者在「系統設定 → 登入項目」關閉
    assert not autostart.is_enabled()
    autostart.set_enabled(True)                       # 在 Divebird 設定中重新勾選
    assert ("enable", f"gui/501/{autostart.MAC_BUNDLE_ID}") in mac.calls
    assert autostart.is_enabled()


def test_macos_refresh_updates_command_but_keeps_disabled_state(mac, tmp_path):
    import plistlib

    autostart.refresh()
    assert _agent(tmp_path)[1] is None, "沒有登記自動啟動時不可自行加上"
    path, _ = _agent(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"Label": autostart.MAC_BUNDLE_ID, "Disabled": True,
                                     "ProgramArguments": ["/Users/me/Downloads/Divebird.app/Contents/MacOS/Divebird",
                                                          "--minimized"]}))
    autostart.refresh()                               # 例如把 Divebird.app 從「下載項目」搬到「應用程式」
    _, data = _agent(tmp_path)
    assert data["ProgramArguments"][0] == "/Applications/Divebird.app/Contents/MacOS/Divebird"
    assert data["Disabled"] is True and not autostart.is_enabled()


def test_macos_translocated_app_cannot_register(mac, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        "/private/var/folders/x/AppTranslocation/1234/d/Divebird.app/Contents/MacOS/Divebird")
    assert "應用程式" in autostart.unavailable_reason()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Divebird"))
    assert autostart.unavailable_reason() == ""


def test_macos_corrupt_launch_agent_is_ignored(mac, tmp_path):
    path, _ = _agent(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'<?xml version="1.0"?><plist version="1.0"><dict><key>Label</key>')    # 寫到一半的檔案
    assert not autostart.is_enabled()
    autostart.refresh()                 # 每次啟動都會呼叫：不可拋出例外
    autostart.set_enabled(True)         # 在設定中重新勾選：直接覆寫成正確的內容
    assert autostart.is_enabled()


def test_macos_has_no_legacy_autostart(mac, tmp_path):
    autostart.migrate_legacy()
    assert _agent(tmp_path)[1] is None and mac.calls == []
