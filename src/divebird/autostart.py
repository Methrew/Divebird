r"""開機 / 登入時自動啟動（縮小到系統匣），讓瀏覽器擴充功能隨時能交付下載。

- Windows：HKCU\Software\Microsoft\Windows\CurrentVersion\Run
  （使用者在「工作管理員 → 啟動應用程式」停用時，Windows 另外記在 StartupApproved\Run）
- macOS：~/Library/LaunchAgents/io.github.acer1204.divebird.plist（launchd 的使用者 LaunchAgent）
  （使用者在「系統設定 → 一般 → 登入項目」關閉時，launchd 會把它標記為停用）
- Linux：~/.config/autostart/divebird.desktop（XDG Autostart）
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from .config import APP_NAME, LEGACY_NAMES, MAC_BUNDLE_ID, running_from_temporary_location

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def launch_command(project: Path | None = None) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    exe = Path(sys.executable)
    if sys.platform == "win32":
        # 從原始碼執行：優先用專案根目錄的 Divebird.exe（scripts/launcher.cs），
        # 它會先檢查執行環境，git pull 改了相依套件時也會先更新再啟動
        project = project or Path(__file__).resolve().parents[2]
        if (project / "Divebird.exe").is_file() and (project / "Divebird.bat").is_file():
            return [str(project / "Divebird.exe"), "--minimized"]
        # 優先用 divebird-gui.exe（CPython 的無視窗 venv 啟動器，見 scripts/win_gui_launcher.py）；
        # uv 建立的 venv 裡 pythonw.exe 其實會開主控台視窗，登入時會跳出黑色視窗
        for name in ("divebird-gui.exe", "pythonw.exe"):
            cand = exe.with_name(name)
            if cand.exists():
                exe = cand
                break
    return [str(exe), "-m", "divebird", "--minimized"]


# ---------------------------------------------------------------- Windows
def _win_run_exists(name: str) -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, name)
            return True
    except OSError:
        return False


def _win_disabled_by_user(name: str) -> bool:
    """工作管理員中被停用的項目：StartupApproved 值的第一個位元組為奇數。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            data, _ = winreg.QueryValueEx(k, name)
            return bool(data) and data[0] % 2 == 1
    except OSError:
        return False


def _win_delete(key: str, name: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


# ---------------------------------------------------------------- macOS
def _launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_BUNDLE_ID}.plist"


def _mac_read() -> dict | None:
    try:
        with open(_launch_agent(), "rb") as f:
            data = plistlib.load(f)
    except Exception:  # noqa: BLE001 - 檔案損毀時 plistlib 可能拋出 ExpatError 等各種例外：視為沒有登記
        return None
    return data if isinstance(data, dict) else None


def _mac_write(cmd: list[str], keep: dict | None = None) -> None:
    data = {
        "Label": MAC_BUNDLE_ID,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "ProcessType": "Interactive",          # 有介面的程式：launchd 不要降低它的優先順序
        "LimitLoadToSessionType": "Aqua",      # 只在使用者登入圖形介面時啟動（不在 SSH 登入時啟動）
        "AssociatedBundleIdentifiers": [MAC_BUNDLE_ID],   # 「登入項目」設定中顯示為 Divebird
    }
    if keep and "Disabled" in keep:
        data["Disabled"] = keep["Disabled"]
    f = _launch_agent()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_bytes(plistlib.dumps(data))
    os.replace(tmp, f)


def _launchctl(*args: str) -> str:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _mac_disabled_by_user() -> bool:
    """使用者在「登入項目」關閉，或以 launchctl disable 停用：launchctl print-disabled 會列出
    "<Label>" => disabled（舊版 macOS 為 => true）。"""
    for line in _launchctl("print-disabled", f"gui/{os.getuid()}").splitlines():
        key, _, value = line.partition("=>")
        if key.strip().strip('"') == MAC_BUNDLE_ID:
            return value.strip().lower() in ("disabled", "true")
    return False


def _mac_enabled() -> bool:
    data = _mac_read()
    return bool(data) and not data.get("Disabled", False) and not _mac_disabled_by_user()


def unavailable_reason() -> str:
    """目前無法設定自動啟動的原因（空字串表示可以）。

    macOS：從磁碟映像檔或「下載項目」直接開啟時，App 在暫存位置執行，登入項目指向那裡，下次登入就找不到程式。"""
    if running_from_temporary_location():
        return "請先把 Divebird 拖到「應用程式」資料夾，再從那裡開啟，才能設定登入時自動啟動。"
    return ""


# ---------------------------------------------------------------- Linux
def _desktop_file(name: str = APP_NAME) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / f"{name.lower()}.desktop"


def desktop_disabled(text: str) -> bool:
    """XDG autostart 項目被停用：Hidden=true 或 X-GNOME-Autostart-enabled=false。"""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip().lower(), value.strip().lower()
        if (key == "hidden" and value == "true") or (key == "x-gnome-autostart-enabled" and value == "false"):
            return True
    return False


def _desktop_enabled(path: Path) -> bool:
    try:
        return not desktop_disabled(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False


# ---------------------------------------------------------------- 公開介面
def is_enabled() -> bool:
    if sys.platform == "win32":
        return _win_run_exists(APP_NAME) and not _win_disabled_by_user(APP_NAME)
    if sys.platform == "darwin":
        return _mac_enabled()
    return _desktop_enabled(_desktop_file())


def set_enabled(enabled: bool) -> None:
    cmd = launch_command()
    if sys.platform == "win32":
        import winreg
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            _win_delete(APPROVED_KEY, APP_NAME)     # 使用者在設定中重新勾選：清除「已停用」標記
        else:
            _win_delete(RUN_KEY, APP_NAME)
        return
    if sys.platform == "darwin":
        if enabled:
            _mac_write(cmd)
            if _mac_disabled_by_user():      # 使用者在設定中重新勾選：清除「已停用」標記
                _launchctl("enable", f"gui/{os.getuid()}/{MAC_BUNDLE_ID}")
        else:
            # 只移除設定檔，不執行 launchctl bootout：由登入項目啟動的 Divebird 就是這個工作，bootout 會把它結束
            _launch_agent().unlink(missing_ok=True)
        return
    f = _desktop_file()
    if enabled:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            "[Desktop Entry]\nType=Application\nName=Divebird\n"
            f"Exec={' '.join(shlex.quote(c) for c in cmd)}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
    else:
        f.unlink(missing_ok=True)


def refresh() -> None:
    """已登記的自動啟動指令與目前的啟動方式不同時，改成目前的指令。
    （Windows：例如後來才有 Divebird.exe；macOS：例如把 Divebird.app 從「下載項目」搬到「應用程式」）
    只改指令本身，不動使用者在工作管理員或登入項目中的「停用」設定。"""
    if sys.platform == "darwin":
        data = _mac_read()
        if data is not None and not unavailable_reason() and data.get("ProgramArguments") != launch_command():
            _mac_write(launch_command(), keep=data)
        return
    if sys.platform != "win32" or not _win_run_exists(APP_NAME):
        return
    import winreg
    cmd = subprocess.list2cmdline(launch_command())
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        current, _ = winreg.QueryValueEx(k, APP_NAME)
    if current != cmd:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)


def migrate_legacy() -> None:
    """改名前（OpenDM）設定的自動啟動：移除舊項目；只有舊項目原本是「啟用中」才以新名稱重新建立，
    不會打開使用者已經在系統中停用的自動啟動。"""
    was_active = False
    if sys.platform == "darwin":
        return      # 改名前的 OpenDM 沒有 macOS 版，沒有舊設定要搬
    if sys.platform == "win32":
        for name in LEGACY_NAMES:
            if _win_run_exists(name):
                was_active = was_active or not _win_disabled_by_user(name)
                _win_delete(RUN_KEY, name)
                _win_delete(APPROVED_KEY, name)
    else:
        for name in LEGACY_NAMES:
            old = _desktop_file(name)
            if old.exists():
                was_active = was_active or _desktop_enabled(old)
                old.unlink()
    if was_active and not is_enabled():
        set_enabled(True)
