"""設定與路徑管理。

資料目錄規則：
- 可攜模式：程式目錄（或打包後執行檔目錄）下存在 ``portable`` 檔案時，資料存放於 ``<程式目錄>/data``
- Windows：%APPDATA%\\Divebird
- macOS：~/Library/Application Support/Divebird
- Linux：$XDG_CONFIG_HOME/divebird（預設 ~/.config/divebird）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "Divebird"
DEFAULT_PORT = 17890
IS_MAC = sys.platform == "darwin"
MAC_BUNDLE_ID = "io.github.acer1204.divebird"     # macOS 的 App 識別碼（Divebird.app、登入自動啟動項目共用）
TRAY_NAME = "選單列" if IS_MAC else "系統匣"      # 介面文字：常駐圖示所在的位置


def app_dir() -> Path:
    """程式所在目錄（打包後為執行檔所在目錄）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """打包後資源解壓目錄（PyInstaller 的 _MEIPASS），開發時為套件目錄。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def running_from_temporary_location() -> bool:
    """macOS：直接從磁碟映像檔（.dmg）或「下載項目」開啟的 Divebird.app，系統會讓它在唯讀的暫存位置執行
    （App Translocation），這時程式所在的路徑之後就會失效。"""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return False
    if "/AppTranslocation/" in sys.executable:
        return True
    try:
        return bool(os.statvfs(sys.executable).f_flag & os.ST_RDONLY)     # 掛載的 .dmg 是唯讀的
    except (OSError, AttributeError):
        return False


def extension_dir() -> Path | None:
    """隨附的 Chrome 擴充功能資料夾（讓使用者在 Chrome「載入未封裝項目」）；找不到時回傳 None。"""
    if getattr(sys, "frozen", False):
        candidates = [app_dir() / "extension"]       # Windows／Linux 免安裝版：程式旁邊
        if IS_MAC:     # Divebird.app/Contents/Resources/extension（app_dir() 是 Contents/MacOS）
            candidates.insert(0, app_dir().parent / "Resources" / "extension")
    else:
        candidates = [Path(__file__).resolve().parents[2] / "extension"]     # 原始碼：專案根目錄
    return next((p for p in candidates if (p / "manifest.json").is_file()), None)


LEGACY_NAMES = ("OpenDM",)   # 改名前的程式名稱：首次啟動時自動搬移舊資料


def _user_data_locations() -> tuple[Path, list[Path]]:
    """（非可攜模式）新資料夾路徑，以及改名前舊名稱的資料夾路徑。"""
    if sys.platform == "win32":
        parent = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return parent / APP_NAME, [parent / n for n in LEGACY_NAMES]
    if IS_MAC:
        parent = Path.home() / "Library" / "Application Support"
        return parent / APP_NAME, [parent / n for n in LEGACY_NAMES]
    parent = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return parent / APP_NAME.lower(), [parent / n.lower() for n in LEGACY_NAMES]


def legacy_ports() -> set[int]:
    """舊版可能使用的 API 埠（預設埠 + 舊設定檔中的埠），用來偵測舊版是否仍在執行。"""
    ports = {DEFAULT_PORT}
    for d in _user_data_locations()[1]:
        try:
            ports.add(int(json.loads((d / "settings.json").read_text(encoding="utf-8"))["port"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return ports


def data_dir() -> Path:
    base = app_dir()
    if (base / "portable").exists():
        path = base / "data"
    else:
        path, legacy = _user_data_locations()
        _migrate_legacy(path, legacy)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _migrate_legacy(path: Path, legacy: list[Path]) -> None:
    """新資料夾還不存在時，複製舊名稱的資料夾（設定、下載清單）。舊資料夾保留不刪，當作備份。

    先複製到暫存資料夾、全部成功才改名為正式資料夾：中途失敗（檔案被鎖住、程式被中斷）時
    不會留下只複製一半的新資料夾，下次啟動會再試一次。"""
    if path.exists():
        try:
            if any(path.iterdir()):
                return
            path.rmdir()    # 空資料夾（例如上次搬移失敗後建立的）：視為尚未搬移
        except OSError:
            return
    for old in legacy:
        if not old.is_dir():
            continue
        tmp = path.with_name(path.name + ".migrating")
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(old, tmp)
            os.replace(tmp, path)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
        return


def default_download_dir() -> Path:
    if sys.platform != "win32" and not IS_MAC:
        # 優先讀取 XDG 使用者目錄設定（中文 Linux 桌面的下載資料夾可能是「下載」）
        try:
            cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
            for line in cfg.read_text(encoding="utf-8").splitlines():
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    value = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(Path.home()))
                    if value:
                        return Path(value)
        except OSError:
            pass
    return Path.home() / "Downloads"


@dataclass
class Settings:
    download_dir: str = field(default_factory=lambda: str(default_download_dir()))
    max_concurrent: int = 3           # 同時進行的下載任務數
    connections: int = 8              # 每個檔案的連線數（分段數上限）
    speed_limit_kbps: int = 0         # 全域限速（KB/s），0 = 不限
    port: int = DEFAULT_PORT          # 本機 API 埠（需與擴充功能設定一致）
    show_dialog: bool = True          # 收到新下載時顯示確認對話框
    minimize_to_tray: bool = True     # 關閉視窗時縮到系統匣
    notify_on_complete: bool = True   # 下載完成時顯示通知
    media_format: str = "bv*+ba/b"    # yt-dlp 預設格式
    merge_format: str = "mp4"         # 影音合併輸出格式
    ffmpeg_path: str = ""             # 自訂 ffmpeg 路徑（空白 = 使用內附）
    max_retries: int = 8              # 每段連線失敗重試次數
    # AI 整合（MCP）：讓支援 MCP 的 AI 工具透過 http://127.0.0.1:<port>/mcp 使用 Divebird。
    # 存取權杖另存於資料夾中的 mcp-token；以下權限只有在啟用 MCP 時才能在設定中修改
    mcp_enabled: bool = False
    mcp_confirm: str = "always"       # always = AI 發起的下載每次在 Divebird 確認；never = 直接下載並通知
    mcp_allow_subdir: bool = True     # AI 可指定下載資料夾底下的子資料夾
    mcp_allow_cookies: bool = False   # AI 可傳入 Cookie 與 Authorization 等登入資訊
    mcp_allow_private: bool = False   # AI 可下載內網與本機位址
    mcp_allow_delete: bool = False    # AI 可刪除已下載的檔案
    mcp_share_browser_media: bool = False   # 把瀏覽器擴充功能偵測到的影音清單提供給 AI（不含 Cookie）

    _lock = threading.Lock()

    @classmethod
    def path(cls) -> Path:
        return data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            for k, v in raw.items():
                if k in names:
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        return s

    def save(self) -> None:
        with self._lock:
            tmp = self.path().with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path())
