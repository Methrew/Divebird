# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包設定（Windows / Linux / macOS 共用）：
#   產出 dist/Divebird/（macOS 另外產出 dist/Divebird.app），內含 Python 執行環境、Qt、yt-dlp，
#   以及 tools/ffmpeg、tools/deno，使用者電腦不需要安裝任何東西即可執行。
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
EXE_SUFFIX = ".exe" if IS_WIN else ""

# ---- 內附外部工具：ffmpeg（影音合併）、deno（yt-dlp 解析 YouTube 需要的 JS 執行環境）
import imageio_ffmpeg
from deno import find_deno_bin

tools = ROOT / "build" / "tools"
tools.mkdir(parents=True, exist_ok=True)
shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), tools / f"ffmpeg{EXE_SUFFIX}")
shutil.copy2(find_deno_bin(), tools / f"deno{EXE_SUFFIX}")
binaries = [(str(tools / f"ffmpeg{EXE_SUFFIX}"), "tools"), (str(tools / f"deno{EXE_SUFFIX}"), "tools")]

datas = collect_data_files("yt_dlp_ejs")
hiddenimports = collect_submodules("yt_dlp_ejs") + ["PySide6.QtSvg"]
if IS_MAC:
    # macOS：擴充功能等檔案放進 Divebird.app/Contents/Resources（Windows / Linux 由建置腳本複製到程式旁邊）。
    # 在這裡加入才會一起被簽章；打包後再複製進 .app 會讓簽章失效
    datas += [(str(ROOT / "extension"), "extension"), (str(ROOT / "skills"), "skills"),
              (str(ROOT / "README.md"), ".")]

a = Analysis(
    [str(ROOT / "src" / "divebird" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "imageio_ffmpeg", "deno", "pytest", "PyInstaller",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtWebEngineCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Divebird",
    console=False,
    icon=str(ROOT / "assets" / "divebird.ico") if IS_WIN else None,
    upx=False,
)
# ---- divebird-mcp：MCP 的 stdio 橋接（主控台程式），給只能以 stdio 啟動 MCP 伺服器的 AI 應用程式使用。
#      只用到標準函式庫與 divebird 的設定、協定模組，不需要 Qt、yt-dlp 與網路套件
b = Analysis(
    [str(ROOT / "src" / "divebird" / "mcp" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    excludes=["tkinter", "PySide6", "yt_dlp", "yt_dlp_ejs", "requests", "urllib3", "imageio_ffmpeg", "deno",
              "pytest", "PyInstaller"],
    noarchive=False,
)
pyz_mcp = PYZ(b.pure)
exe_mcp = EXE(
    pyz_mcp,
    b.scripts,
    [],
    exclude_binaries=True,
    name="divebird-mcp",
    console=True,
    icon=str(ROOT / "assets" / "divebird.ico") if IS_WIN else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, exe_mcp, b.binaries, b.datas, name="Divebird", upx=False)

if IS_MAC:
    sys.path.insert(0, str(ROOT / "src"))
    from divebird import __version__
    from divebird.config import MAC_BUNDLE_ID

    def qt_min_macos(default: str = "13.0") -> str:
        """內附的 Qt 需要的最低 macOS 版本（QtCore 的 LC_BUILD_VERSION）。Qt 在較舊的系統上啟動時會直接結束，
        寫進 Info.plist 後，舊系統會改為顯示「需要較新版本的 macOS」。"""
        try:
            import PySide6
            from macholib.MachO import MachO
            from macholib.mach_o import LC_BUILD_VERSION

            qtcore = Path(PySide6.__file__).parent / "Qt" / "lib" / "QtCore.framework" / "Versions" / "A" / "QtCore"
            found = [(int(cmd.minos) >> 16, (int(cmd.minos) >> 8) & 0xFF)
                     for header in MachO(str(qtcore)).headers
                     for load_cmd, cmd, _data in header.commands if load_cmd.cmd == LC_BUILD_VERSION]
            if found:
                return "%d.%d" % max(found)
        except Exception:  # noqa: BLE001
            pass
        return default

    folder_use = "Divebird 會把下載的檔案存到你選擇的資料夾。"
    app = BUNDLE(
        coll,
        name="Divebird.app",
        icon=str(ROOT / "assets" / "divebird.icns"),
        bundle_identifier=MAC_BUNDLE_ID,
        version=__version__,
        info_plist={
            "CFBundleName": "Divebird",
            "CFBundleDisplayName": "Divebird",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": qt_min_macos(),
            # 介面只有繁體中文：系統提供的介面（例如選擇資料夾的視窗）也用繁體中文，不會中英混雜
            "CFBundleDevelopmentRegion": "zh_TW",
            "CFBundleLocalizations": ["zh_TW"],
            # 主視窗縮到選單列時 Divebird 仍在背景下載：不要讓 App Nap 降低它的 CPU 與網路優先順序
            "LSAppNapIsDisabled": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
            # 一般的 App（有 Dock 圖示與選單列）。COLLECT 裡最後一個 EXE（divebird-mcp）是主控台程式，
            # PyInstaller 會因此預設 LSBackgroundOnly=True，讓 Divebird 變成無法切到前景的背景程式
            "LSBackgroundOnly": False,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,       # 跟隨系統的淺色／深色外觀
            "NSHumanReadableCopyright": "Copyright © acer1204. MIT License.",
            # 第一次存取這些位置時，系統詢問視窗中顯示的說明
            "NSDownloadsFolderUsageDescription": folder_use,
            "NSDesktopFolderUsageDescription": folder_use,
            "NSDocumentsFolderUsageDescription": folder_use,
            "NSRemovableVolumesUsageDescription": folder_use,
            "NSNetworkVolumesUsageDescription": folder_use,
            "NSLocalNetworkUsageDescription": "Divebird 從區域網路上的伺服器下載檔案時需要這項權限。",
        },
    )
