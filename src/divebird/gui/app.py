from __future__ import annotations

import os
import sys
import threading

import requests
from PySide6.QtCore import QEvent, QLibraryInfo, QObject, Qt, QTimer, QTranslator, Signal
from PySide6.QtGui import QAction, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .. import autostart
from ..config import APP_NAME, IS_MAC, LEGACY_NAMES, Settings, legacy_ports
from ..engine.manager import DownloadManager
from ..engine.media_engine import _site_extractors
from ..intake import task_from_payload
from ..localport import listening
from ..mcp import token as mcp_token
from ..mcp.service import McpService
from ..models import Task
from ..server import ApiServer
from . import icons
from .dialogs import NewDownloadDialog
from .main_window import MainWindow


class Bridge(QObject):
    """把下載引擎（背景執行緒）的事件轉成 Qt 訊號，安全地送回 GUI 執行緒。"""

    added = Signal(object)
    removed = Signal(str)
    changed = Signal(object)
    finished = Signal(object)
    download_requested = Signal(object, bool)   # (Task, 是否詢問)
    show_requested = Signal()
    mcp_confirm_requested = Signal(object, bool, str)   # (Task, 立即開始, AI 工具名稱)
    notify_requested = Signal(str, str)

    def task_added(self, t):
        self.added.emit(t)

    def task_removed(self, tid):
        self.removed.emit(tid)

    def task_changed(self, t):
        self.changed.emit(t)

    def task_finished(self, t):
        self.finished.emit(t)


def is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def stylesheet(dark: bool) -> str:
    line = "#2b3544" if dark else "#e5e9f0"
    side = "#161b22" if dark else "#f6f8fb"
    hover = "#253041" if dark else "#e9eef6"
    sel_bg = "#1e3a6e" if dark else "#dbe7fe"
    sel_fg = "#e2e8f0" if dark else "#0f172a"
    accent = "#60a5fa" if dark else "#1d4ed8"
    # 明確定義核取方塊外觀：部分深色主題下 Fusion 會把「未勾選」也畫成整塊強調色，難以分辨
    box_bg = "#111827" if dark else "#ffffff"
    box_border = "#4b5563" if dark else "#9ca3af"
    check = icons.check_mark_path()
    return f"""
    QToolBar#main-toolbar {{ border: none; border-bottom: 1px solid {line}; spacing: 2px; padding: 4px 8px; }}
    QToolBar#main-toolbar QToolButton {{ padding: 4px 8px; border-radius: 6px; min-width: 56px; }}
    QToolBar#main-toolbar QToolButton:hover {{ background: {hover}; }}
    QToolBar#main-toolbar QToolButton:disabled {{ color: palette(placeholder-text); }}
    QLineEdit#search {{ padding: 5px 8px; border-radius: 6px; }}
    QListWidget#categories {{ border: none; background: {side}; padding-top: 6px; outline: 0; }}
    QListWidget#categories::item {{ padding: 7px 8px; margin: 1px 6px; border-radius: 6px; }}
    QListWidget#categories::item:hover {{ background: {hover}; }}
    QListWidget#categories::item:selected {{ background: {sel_bg}; color: {accent}; }}
    QTableView#tasks {{ border: none; outline: 0; selection-background-color: {sel_bg};
                        selection-color: {sel_fg}; }}
    QTableView#tasks::item {{ border-bottom: 1px solid {line}; padding-left: 6px; }}
    QTableView#tasks::item:selected {{ background: {sel_bg}; color: {sel_fg}; }}
    QHeaderView::section {{ border: none; border-bottom: 1px solid {line}; padding: 6px 8px;
                           background: palette(base); font-weight: 600; }}
    QSplitter::handle {{ background: {line}; width: 1px; }}
    QStatusBar {{ border-top: 1px solid {line}; }}
    QStatusBar QLabel {{ padding: 2px 6px; }}
    QPushButton#primary {{ background: #2563eb; color: white; border: none; padding: 6px 18px;
                          border-radius: 6px; font-weight: 600; }}
    QPushButton#primary:hover {{ background: #1d4ed8; }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {box_border};
                           background: {box_bg}; }}
    QCheckBox::indicator:hover {{ border-color: {accent}; }}
    QCheckBox::indicator:checked {{ background: #2563eb; border-color: #2563eb; image: url({check}); }}
    QCheckBox::indicator:disabled {{ background: transparent; border-color: {line}; }}
    """


class _QuitWatcher(QObject):
    """macOS：Dock 的「結束」、應用程式選單的「結束」（⌘Q）、登出與關機，都是由系統送 Quit 事件給程式，
    不會經過 Controller.quit()。沒有這個處理時，主視窗會被當成一般的關閉：縮到選單列、跳出「仍在背景執行」
    的通知，接著程式就結束了，下載沒有先暫停、儲存。這裡先做跟「結束 Divebird」相同的收尾。"""

    def __init__(self, ctl: "Controller"):
        super().__init__(ctl)
        self.ctl = ctl

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.ctl.app and event.type() == QEvent.Type.Quit:
            self.ctl.prepare_quit()
        return False


class Controller(QObject):
    def __init__(self, app: QApplication, settings: Settings):
        super().__init__()
        self.app = app
        self.settings = settings
        self.dark = is_dark()
        self.bridge = Bridge()
        self.manager = DownloadManager(settings, listener=self.bridge)
        self._dialogs: set[NewDownloadDialog] = set()
        self.tray: QSystemTrayIcon | None = None
        self._notified_task = ""      # 最近一則通知對應的下載（點通知時選取它）
        self._closing = False

        # AI 整合（MCP）：工具在 API 執行緒執行，需要介面的動作（確認視窗、通知）透過訊號回到 GUI 執行緒
        self.mcp = McpService(settings, backend=self)
        if settings.mcp_enabled:
            mcp_token.ensure()
        self.server: ApiServer | None = None
        self.api_ok = False
        self._start_server()

        self.bridge.download_requested.connect(self.handle_request)
        self.bridge.show_requested.connect(lambda: self.window.bring_to_front())
        self.bridge.mcp_confirm_requested.connect(self._open_mcp_dialog)
        self.bridge.notify_requested.connect(self.notify)
        self.bridge.finished.connect(self._on_finished)

        self.window = MainWindow(self)
        # 系統在執行中切換淺色／深色外觀（例如 macOS 的「自動」外觀在日落時切換）：重新著色
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._color_scheme_changed)
        self._setup_tray()
        if IS_MAC:
            self._quit_watcher = _QuitWatcher(self)
            app.installEventFilter(self._quit_watcher)
            # 主視窗關著時點 Dock 圖示：Qt 會送出「程式被啟用」。啟動時 Qt 也會把程式帶到前景一次，
            # 所以等事件迴圈開始一秒後才開始處理，--minimized 啟動時才不會自己打開主視窗
            QTimer.singleShot(0, lambda: QTimer.singleShot(
                1000, lambda: app.applicationStateChanged.connect(self._app_state_changed)))
        # 預先載入 yt-dlp 的網站清單，讓第一次判斷網址類型時不會卡頓
        threading.Thread(target=_site_extractors, daemon=True).start()

    # ---------------------------------------------------------------- API
    def _start_server(self):
        self.server = ApiServer(self.settings.port, on_download=self._api_download,
                                on_show=self.bridge.show_requested.emit, mcp=self.mcp)
        self.api_ok = self.server.start()

    def restart_server(self):
        if self.server:
            self.server.stop()
        self._start_server()

    def _api_download(self, payload: dict):
        """由 API 執行緒呼叫：另開執行緒判斷網址類型，讓擴充功能立即收到回應。"""
        if payload.get("mcp_request"):
            # 擴充功能補上登入資訊後送來的、AI 指定的下載（list_browser_media → download_browser_media）
            threading.Thread(target=self.mcp.tools.fulfill_browser_request, args=(payload,), daemon=True).start()
            return

        def work():
            self.bridge.download_requested.emit(task_from_payload(payload, self.settings), True)
        threading.Thread(target=work, daemon=True).start()

    def request_url(self, url: str, ask: bool = True):
        def work():
            task = task_from_payload({"url": url}, self.settings)
            self.bridge.download_requested.emit(task, ask)
        threading.Thread(target=work, daemon=True).start()

    def handle_request(self, task: Task, ask: bool):
        if ask and self.settings.show_dialog:
            dlg = NewDownloadDialog(task, self.settings)
            dlg.submitted.connect(lambda t, start: self.manager.add(t, start))
            dlg.finished.connect(lambda _r, d=dlg: self._dialogs.discard(d))
            self._dialogs.add(dlg)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        else:
            self.manager.add(task)
            if not self.window.isVisible():
                self.notify("已開始下載", task.filename or task.title or task.url, task.id)

    # ---------------------------------------------------------------- AI 整合（MCP）的介面動作
    def confirm(self, task: Task, start: bool, client: str) -> None:
        """由 API 執行緒呼叫：在 Divebird 跳出確認視窗。"""
        self.bridge.mcp_confirm_requested.emit(task, start, client)

    def announce(self, title: str, message: str) -> None:
        """由 API 執行緒呼叫：顯示系統匣通知。"""
        self.bridge.notify_requested.emit(title, message)

    def _open_mcp_dialog(self, task: Task, start: bool, client: str) -> None:
        dlg = NewDownloadDialog(task, self.settings, origin=client, start=start)

        def submitted(t: Task, start_now: bool) -> None:
            self.manager.add(t, start_now)
            self.mcp.tools.confirmation_done(t.id, True)

        dlg.submitted.connect(submitted)
        dlg.finished.connect(lambda _r, d=dlg: (self._dialogs.discard(d),
                                                d.was_submitted or self.mcp.tools.confirmation_done(task.id, False)))
        self._dialogs.add(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # ---------------------------------------------------------------- 系統匣
    def tray_available(self) -> bool:
        return self.tray is not None and QSystemTrayIcon.isSystemTrayAvailable()

    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(icons.tray_icon(), self)
        menu = QMenu()
        a_show = QAction("顯示主視窗", menu)
        a_show.triggered.connect(self.window.bring_to_front)
        menu.addAction(a_show)
        menu.addSeparator()
        menu.addAction("全部開始", self.manager.start_all)
        menu.addAction("全部暫停", self.manager.pause_all)
        menu.addSeparator()
        menu.addAction("關於 Divebird", self.window.open_about)
        menu.addAction("結束 Divebird", self.quit)
        tray.setContextMenu(menu)
        if not IS_MAC:      # macOS 點選單列圖示是打開選單（第一項就是「顯示主視窗」），不另外叫出視窗
            tray.activated.connect(lambda reason: reason in (QSystemTrayIcon.ActivationReason.Trigger,
                                                             QSystemTrayIcon.ActivationReason.DoubleClick)
                                   and self.window.bring_to_front())
        tray.messageClicked.connect(self._notification_clicked)
        tray.setToolTip(APP_NAME)
        tray.show()
        self._tray_menu = menu
        self.tray = tray

    def update_tray_tooltip(self, active: int, speed: float):
        if self.tray:
            from ..utils import human_size
            self.tray.setToolTip(f"{APP_NAME} — 下載中 {active} 個，{human_size(speed)}/s" if active else APP_NAME)

    def notify(self, title: str, msg: str, task_id: str = ""):
        if self.tray:
            self._notified_task = task_id     # Qt 只告訴我們「有通知被點了」，以最近一則為準
            self.tray.showMessage(title, msg, icons.app_icon(), 4000)

    def _notification_clicked(self) -> None:
        """點通知：打開主視窗；通知是某筆下載的話，選取那一筆。"""
        self.window.bring_to_front()
        if self._notified_task:
            self.window.select_task(self._notified_task)

    def _on_finished(self, t: Task):
        if self.settings.notify_on_complete:
            self.notify("下載完成", t.filename, t.id)

    def _color_scheme_changed(self, *_args) -> None:
        # Qt 發出這個訊號時，元件的調色盤可能還沒換成新的外觀（樣式表裡的 palette(base) 會解析成舊的顏色），
        # 等這一輪事件處理完再重新套用
        QTimer.singleShot(0, self._apply_color_scheme)

    def _apply_color_scheme(self) -> None:
        dark = is_dark()
        if dark == self.dark:
            return
        self.dark = dark
        self.app.setStyleSheet(stylesheet(dark))
        self.window.apply_theme(dark)

    def _app_state_changed(self, state) -> None:
        """macOS：沒有任何視窗開著時點 Dock 圖示（或再開一次 Divebird.app），打開主視窗。"""
        if state != Qt.ApplicationState.ApplicationActive or self._closing:
            return
        if not any(w.isVisible() for w in QApplication.topLevelWidgets() if w.isWindow()):
            self.window.bring_to_front()

    def prepare_quit(self) -> None:
        """結束前的收尾（只做一次）：關閉對話框、暫停並儲存下載、停止本機 API。"""
        if self._closing:
            return
        self._closing = True
        self.window._quitting = True
        for d in list(self._dialogs):
            d.close()
        self.manager.shutdown()
        if self.server:
            self.server.stop()
        if self.tray:
            self.tray.hide()

    def quit(self):
        if self._closing:       # 已在結束流程中（主視窗關閉時會再呼叫一次）
            return
        self.prepare_quit()
        self.app.quit()


_listening = listening     # 舊名稱（測試仍在使用）


def _forward_to_running(port: int, urls: list[str]) -> bool:
    """若已有 Divebird 在執行，把網址交給它並喚出視窗。"""
    if not _listening(port):
        return False
    base = f"http://127.0.0.1:{port}"
    try:
        r = requests.get(base + "/api/ping", timeout=(0.5, 3))
        if not (r.ok and r.json().get("app") == APP_NAME):
            return False
        for u in urls:
            requests.post(base + "/api/download", json={"url": u}, timeout=3)
        requests.post(base + "/api/show", json={}, timeout=3)
        return True
    except (requests.RequestException, ValueError):
        return False


def _running_legacy_app() -> str | None:
    """改名前的舊版（OpenDM）是否仍在執行：回傳它回報的名稱。"""
    for port in sorted(legacy_ports()):
        if not _listening(port):
            continue
        try:
            name = requests.get(f"http://127.0.0.1:{port}/api/ping", timeout=(0.5, 2)).json().get("app")
        except (requests.RequestException, ValueError, AttributeError):
            continue
        if name in LEGACY_NAMES:
            return name
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    urls = [a for a in argv[1:] if a.lower().startswith(("http://", "https://"))]
    # 必須在讀取設定（會觸發舊資料搬移）之前檢查：舊版還在寫入資料時不能複製
    legacy = _running_legacy_app()
    if legacy:
        app = QApplication(argv)
        app.setWindowIcon(icons.app_icon())
        QMessageBox.warning(None, APP_NAME,
                            f"偵測到舊版 {legacy} 仍在執行。\n\n"
                            f"請先結束 {legacy}（系統匣圖示按右鍵 → 結束），再重新啟動 {APP_NAME}，\n"
                            f"設定與下載清單才能完整搬移，瀏覽器整合也才能正常運作。")
        return 1
    settings = Settings.load()
    if _forward_to_running(settings.port, urls):
        return 0

    if sys.platform == "win32":
        try:   # 讓 Windows 工作列使用本程式的圖示而非 python.exe
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Divebird.DownloadManager")
        except Exception:  # noqa: BLE001
            pass
    try:   # 改名前（OpenDM）設定的開機自動啟動，換成新名稱
        autostart.migrate_legacy()
    except OSError:
        pass
    try:   # 自動啟動指令跟上目前的啟動方式（例如改用專案根目錄的 Divebird.exe）
        autostart.refresh()
    except OSError:
        pass

    if IS_MAC and "--minimized" in argv and getattr(sys, "frozen", False):
        # Divebird.app 在背景啟動（登入時、AI 工具經由 divebird-mcp 啟動）：不要讓 Qt 在啟動時把程式切到前景、
        # 搶走使用者正在用的 App 的鍵盤焦點。只限 .app：從原始碼執行時，Qt 要靠這一步把 python 轉成一般的 App
        os.environ.setdefault("QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM", "1")
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(APP_NAME)
    # macOS：從原始碼執行（沒有 Divebird.app）時，Qt 以 argv[0] 的檔名當作應用程式選單裡的名稱
    # （python -m divebird 會變成「結束 __main__」），改成 Divebird；Linux 的 argv[0] 會影響 WM_CLASS，不更動
    app = QApplication([APP_NAME, *argv[1:]] if IS_MAC else argv)
    if IS_MAC:
        # macOS 的應用程式選單（關於、偏好設定、隱藏、結束 Divebird）由 Qt 產生：載入 Qt 的繁體中文翻譯
        translator = QTranslator(app)
        if translator.load("qtbase_zh_TW", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(translator)
    app.setStyle("Fusion")
    app.setWindowIcon(icons.app_icon())
    app.setQuitOnLastWindowClosed(False)

    ctl = Controller(app, settings)
    app.setStyleSheet(stylesheet(ctl.dark))
    if "--minimized" not in argv:
        ctl.window.show()
    elif not ctl.tray_available():
        ctl.window.showMinimized()     # 沒有系統匣時至少留在工作列
    for u in urls:
        ctl.request_url(u)
    return app.exec()
