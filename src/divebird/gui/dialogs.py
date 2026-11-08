from __future__ import annotations

import copy
import json
import os
import threading

import requests
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from .. import __version__, about, autostart
from ..config import IS_MAC, TRAY_NAME, Settings, extension_dir, running_from_temporary_location
from ..engine.http_engine import make_session, probe
from ..engine.media_engine import AUDIO_BEST, AUDIO_MP3, extract_info, format_choices
from ..engine.tools import deno_path, ffmpeg_path
from ..mcp import token as mcp_token
from ..models import Kind, Task
from ..utils import human_size, human_time, open_path, sanitize_filename
from . import icons

class _Signals(QObject):
    http_ready = Signal(object)
    media_ready = Signal(object)
    failed = Signal(str)
    thumb_ready = Signal(bytes)


def _elide_middle(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[: n // 2 - 2] + " … " + text[-n // 2 + 2:]


class NewDownloadDialog(QDialog):
    """收到新下載時的確認對話框：顯示檔案資訊、選擇畫質與儲存位置。"""

    submitted = Signal(object, bool)      # (Task, 立即開始)

    def __init__(self, task: Task, settings: Settings, parent=None, *, origin: str = "", start: bool = True):
        """origin：發起下載的 AI 工具名稱（透過 MCP），空白表示由使用者或瀏覽器擴充功能發起。"""
        super().__init__(parent)
        self.task = task
        self.settings = settings
        self._closed = False
        self.was_submitted = False
        self.origin = origin
        self.setWindowTitle("新增下載（AI 發起）" if origin else "新增下載")
        self.setWindowIcon(icons.app_icon())
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(620)

        self.sig = _Signals()
        self.sig.http_ready.connect(self._on_http)
        self.sig.media_ready.connect(self._on_media)
        self.sig.failed.connect(self._on_failed)
        self.sig.thumb_ready.connect(self._on_thumb)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        # 標頭
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icons.app_icon().pixmap(40, 40))
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        title_box = QVBoxLayout()
        title = QLabel("下載檔案資訊")
        f = title.font()
        f.setPointSizeF(f.pointSizeF() + 3)
        f.setWeight(QFont.Weight.DemiBold)
        title.setFont(f)
        self.url_label = QLabel(_elide_middle(task.url))
        self.url_label.setToolTip(task.url)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url_label.setStyleSheet("color: palette(placeholder-text);")
        title_box.addWidget(title)
        title_box.addWidget(self.url_label)
        head.addLayout(title_box, 1)
        root.addLayout(head)

        if origin:
            notice = QLabel(f"這個下載由 AI 工具「{origin}」透過 MCP 發起。請確認網址與檔名無誤後再開始；"
                            "若不是你要求的下載，請按「取消」。")
            notice.setTextFormat(Qt.TextFormat.PlainText)
            notice.setWordWrap(True)
            notice.setStyleSheet("background: rgba(37, 99, 235, 0.12); border-radius: 6px; padding: 8px 10px;")
            root.addWidget(notice)

        body = QHBoxLayout()
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.name_edit = QLineEdit(task.filename)
        self.name_edit.setPlaceholderText("解析中…")
        form.addRow("檔案名稱", self.name_edit)

        self.quality = QComboBox()
        self.quality.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.quality.currentIndexChanged.connect(self._sync_ext)
        self.quality_label = QLabel("畫質")
        form.addRow(self.quality_label, self.quality)

        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(task.save_dir or settings.download_dir)
        browse = QPushButton("瀏覽…")
        browse.clicked.connect(self._browse)
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        form.addRow("儲存至", dir_row)

        self.info_label = QLabel("正在取得檔案資訊…")
        self.info_label.setWordWrap(True)
        form.addRow("資訊", self.info_label)
        body.addLayout(form, 1)

        self.thumb = QLabel()
        self.thumb.setFixedSize(176, 99)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dark = self.palette().window().color().lightness() < 128
        self.thumb.setStyleSheet(f"background: {'#111827' if dark else '#eef2f7'}; border-radius: 8px;")
        self.thumb.setPixmap(icons.icon("video", "#94a3b8").pixmap(36, 36))
        body.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(body)

        self.skip_box = QCheckBox("不再顯示此視窗，直接開始下載（可在設定中更改）")
        root.addWidget(self.skip_box)
        # AI 發起的下載是否要確認，由「設定 → AI 整合」另外決定，不在這裡關閉
        self.skip_box.setVisible(not origin)

        buttons = QDialogButtonBox()
        self.start_btn = buttons.addButton("開始下載", QDialogButtonBox.ButtonRole.AcceptRole)
        self.later_btn = buttons.addButton("稍後下載", QDialogButtonBox.ButtonRole.ActionRole)
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.start_btn.setDefault(start)
        self.later_btn.setDefault(not start)
        self.start_btn.setIcon(icons.icon("downloading", "#ffffff"))
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(lambda: self._submit(True))
        self.later_btn.clicked.connect(lambda: self._submit(False))
        cancel.clicked.connect(self.reject)
        root.addWidget(buttons)

        self._set_media_mode(task.kind == Kind.MEDIA)
        self._analyze()

    # ---------------------------------------------------------------- 介面
    def _set_media_mode(self, media: bool) -> None:
        self.quality.setVisible(media)
        self.quality_label.setVisible(media)
        self.thumb.setVisible(media)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "選擇儲存資料夾", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _sync_ext(self):
        fmt = self.quality.currentData()
        if fmt is None or self.task.kind != Kind.MEDIA:
            return
        ext = ".mp3" if fmt == AUDIO_MP3 else ".m4a" if fmt == AUDIO_BEST else "." + self.settings.merge_format
        stem = os.path.splitext(self.name_edit.text())[0] if self.name_edit.text() else ""
        if stem:
            self.name_edit.setText(stem + ext)

    def _submit(self, start: bool):
        t = self.task
        t.filename = sanitize_filename(self.name_edit.text()) if self.name_edit.text().strip() else ""
        t.save_dir = self.dir_edit.text().strip() or self.settings.download_dir
        if t.kind == Kind.MEDIA and self.quality.currentData():
            t.media_format = self.quality.currentData()
        if not self.origin and self.skip_box.isChecked():
            self.settings.show_dialog = False
            self.settings.save()
        self.was_submitted = True
        self.submitted.emit(t, start)
        self.accept()

    def done(self, r):
        self._closed = True
        super().done(r)

    # ---------------------------------------------------------------- 背景解析
    def _analyze(self):
        task = copy.copy(self.task)
        settings = self.settings
        sig = self.sig

        def work():
            try:
                if task.kind == Kind.MEDIA:
                    sig.media_ready.emit(extract_info(task, settings))
                    return
                session = make_session(task)
                try:
                    res = probe(session, task.url)
                finally:
                    session.close()
                if res.manifest:
                    task.kind = Kind.MEDIA
                    sig.http_ready.emit(res)
                    sig.media_ready.emit(extract_info(task, settings))
                else:
                    sig.http_ready.emit(res)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if isinstance(e, requests.HTTPError) and e.response is not None:
                    msg = f"HTTP {e.response.status_code}"
                sig.failed.emit(msg)

        threading.Thread(target=work, daemon=True).start()

    def _on_http(self, res):
        if self._closed:
            return
        if res.manifest:
            self.task.kind = Kind.MEDIA
            self._set_media_mode(True)
            self.info_label.setText("偵測到串流播放清單，正在解析影片…")
            return
        if not self.name_edit.text():
            self.name_edit.setText(res.filename)
        size = human_size(res.total) if res.total > 0 else "未知大小"
        resume = "支援續傳、多連線加速" if res.resumable else "不支援續傳（單一連線）"
        ctype = res.content_type.split(";")[0] or "未知類型"
        self.info_label.setText(f"<b>{size}</b>　·　{ctype}<br>{resume}")

    def _on_media(self, info: dict):
        if self._closed:
            return
        self._set_media_mode(True)
        title = info.get("title") or ""
        if not self.name_edit.text():
            # 直接的 m3u8/mpd 網址 yt-dlp 只能拿到無意義的檔名，優先使用網頁標題
            generic = info.get("extractor_key") == "Generic"
            name = (self.task.title if generic and self.task.title else title) or "video"
            self.name_edit.setText(sanitize_filename(name) + "." + self.settings.merge_format)
        self.quality.blockSignals(True)
        self.quality.clear()
        for label, fmt in format_choices(info):
            self.quality.addItem(label, fmt)
        # 套用設定中的預設畫質
        idx = self.quality.findData(self.settings.media_format)
        self.quality.setCurrentIndex(max(0, idx))
        self.quality.blockSignals(False)
        self._sync_ext()

        parts = []
        if info.get("extractor_key") and info["extractor_key"] != "Generic":
            parts.append(info["extractor_key"])
        if info.get("duration"):
            parts.append(f"長度 {human_time(info['duration'])}")
        if info.get("uploader"):
            parts.append(info["uploader"])
        self.info_label.setText(f"<b>{title}</b><br>{'　·　'.join(parts)}" if title else "　·　".join(parts))
        thumb = info.get("thumbnail")
        if thumb:
            def fetch():
                try:
                    r = requests.get(thumb, timeout=10, headers={"User-Agent": self.task.user_agent or "Mozilla/5.0"})
                    if r.ok:
                        self.sig.thumb_ready.emit(r.content)
                except requests.RequestException:
                    pass
            threading.Thread(target=fetch, daemon=True).start()

    def _on_thumb(self, data: bytes):
        if self._closed:
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            self.thumb.setPixmap(pm.scaled(self.thumb.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                           Qt.TransformationMode.SmoothTransformation)
                                 .copy(0, 0, self.thumb.width(), self.thumb.height()))

    def _on_failed(self, msg: str):
        if self._closed:
            return
        self.info_label.setText(f"<span style='color:#dc2626'>無法取得檔案資訊：{msg}</span>"
                                "<br>仍可加入下載，稍後重試。")
        if not self.name_edit.text():
            self.name_edit.setPlaceholderText("（自動）")
        if self.task.kind == Kind.MEDIA and self.quality.count() == 0:
            self.quality.addItem("最佳品質（自動）", self.settings.media_format)


class AddUrlDialog(QDialog):
    def __init__(self, parent=None, initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle("新增網址")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("輸入下載網址（一行一個，可為檔案連結、m3u8 / mpd 串流或影音網站網址）："))
        self.edit = QPlainTextEdit(initial)
        self.edit.setFixedHeight(110)
        lay.addWidget(self.edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("下一步")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def urls(self) -> list[str]:
        return [u.strip() for u in self.edit.toPlainText().splitlines()
                if u.strip().lower().startswith(("http://", "https://"))]


class DeleteDialog(QDialog):
    def __init__(self, count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("刪除下載")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"確定要從清單中移除 {count} 個項目？"))
        self.delete_files = QCheckBox("同時刪除已下載的檔案")
        lay.addWidget(self.delete_files)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        bb.button(QDialogButtonBox.StandardButton.Yes).setText("刪除")
        bb.button(QDialogButtonBox.StandardButton.No).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("設定")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # ---------------------------------------------------------------- 一般
        general = QWidget()
        gv = QVBoxLayout(general)
        g1 = QGroupBox("下載")
        f1 = QFormLayout(g1)
        row = QHBoxLayout()
        self.dir_edit = QLineEdit(settings.download_dir)
        b = QPushButton("瀏覽…")
        b.clicked.connect(lambda: self._pick_dir(self.dir_edit))
        row.addWidget(self.dir_edit, 1)
        row.addWidget(b)
        f1.addRow("預設下載資料夾", row)
        self.concurrent = self._spin(1, 16, settings.max_concurrent)
        f1.addRow("同時下載任務數", self.concurrent)
        self.connections = self._spin(1, 32, settings.connections)
        f1.addRow("每個檔案的連線數", self.connections)
        self.speed = self._spin(0, 10_000_000, settings.speed_limit_kbps, " KB/s")
        self.speed.setSpecialValueText("不限速")
        f1.addRow("全域限速", self.speed)
        gv.addWidget(g1)

        g4 = QGroupBox("介面")
        v4 = QVBoxLayout(g4)
        self.tray = QCheckBox(f"關閉視窗時縮小到{TRAY_NAME}，繼續在背景下載")
        self.tray.setChecked(settings.minimize_to_tray)
        self.notify = QCheckBox("下載完成時顯示通知")
        self.notify.setChecked(settings.notify_on_complete)
        self.autostart = QCheckBox(f"登入系統時自動啟動（縮小到{TRAY_NAME}）")
        self.autostart.setChecked(autostart.is_enabled())
        v4.addWidget(self.tray)
        v4.addWidget(self.notify)
        v4.addWidget(self.autostart)
        reason = autostart.unavailable_reason()
        if reason:      # macOS：從磁碟映像檔或「下載項目」直接開啟時，系統讓 App 在暫存位置執行
            self.autostart.setEnabled(False)
            note = QLabel(reason)
            note.setWordWrap(True)
            note.setStyleSheet("color: palette(placeholder-text);")
            v4.addWidget(note)
        gv.addWidget(g4)
        gv.addStretch(1)
        tabs.addTab(general, "一般")

        # ---------------------------------------------------------------- 瀏覽器整合
        browser = QWidget()
        bv = QVBoxLayout(browser)
        g2 = QGroupBox("瀏覽器整合")
        f2 = QFormLayout(g2)
        self.port = self._spin(1024, 65535, settings.port)
        self.port.setGroupSeparatorShown(False)
        f2.addRow("本機 API 埠號", self.port)
        hint = QLabel("需與 Chrome 擴充功能設定的埠號相同（預設 17890）。")
        hint.setStyleSheet("color: palette(placeholder-text);")
        f2.addRow("", hint)
        self.show_dialog = QCheckBox("收到瀏覽器傳來的下載時，顯示確認視窗")
        self.show_dialog.setChecked(settings.show_dialog)
        f2.addRow("", self.show_dialog)
        ext = extension_dir()
        if ext:     # 擴充功能要從這個資料夾手動載入；macOS 版放在 Divebird.app 裡面，不容易找到
            row = QHBoxLayout()
            show = QPushButton("在 Finder 中顯示" if IS_MAC else "開啟所在資料夾")
            show.clicked.connect(lambda: open_path(ext, select=True))
            copy_path = QPushButton("複製路徑")
            copy_path.clicked.connect(lambda: self._copy(str(ext), copy_path))
            row.addWidget(show)
            row.addWidget(copy_path)
            row.addStretch(1)
            f2.addRow("Chrome 擴充功能", row)
            how = ("在 chrome://extensions 打開「開發人員模式」，按「載入未封裝項目」，"
                   + ("按 ⌘⇧G 貼上路徑，選擇這個資料夾。" if IS_MAC else "選擇這個資料夾。"))
            if running_from_temporary_location():   # 從 .dmg 或「下載項目」直接開啟：這個路徑之後會失效
                show.setEnabled(False)
                copy_path.setEnabled(False)
                how = ("Divebird 正從磁碟映像檔或暫存位置執行，這個路徑之後會失效。"
                       "請先把 Divebird 拖到「應用程式」資料夾，再從那裡開啟。")
            ext_hint = QLabel(how)
            ext_hint.setWordWrap(True)
            ext_hint.setStyleSheet("color: palette(placeholder-text);")
            f2.addRow("", ext_hint)
        bv.addWidget(g2)
        bv.addStretch(1)
        tabs.addTab(browser, "瀏覽器整合")

        # ---------------------------------------------------------------- 影音
        media = QWidget()
        mv = QVBoxLayout(media)
        g3 = QGroupBox("影音（yt-dlp）")
        f3 = QFormLayout(g3)
        self.quality = QComboBox()
        for label, fmt in [("最佳畫質", "bv*+ba/b"),
                           ("最高 2160p", "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b"),
                           ("最高 1080p", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"),
                           ("最高 720p", "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"),
                           ("最高 480p", "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b")]:
            self.quality.addItem(label, fmt)
        self.quality.setCurrentIndex(max(0, self.quality.findData(settings.media_format)))
        f3.addRow("預設畫質", self.quality)
        self.merge = QComboBox()
        self.merge.addItems(["mp4", "mkv"])
        self.merge.setCurrentText(settings.merge_format)
        f3.addRow("影音合併格式", self.merge)
        row = QHBoxLayout()
        self.ffmpeg = QLineEdit(settings.ffmpeg_path)
        self.ffmpeg.setPlaceholderText("留白 = 使用內附 ffmpeg")
        b2 = QPushButton("瀏覽…")
        b2.clicked.connect(self._pick_ffmpeg)
        row.addWidget(self.ffmpeg, 1)
        row.addWidget(b2)
        f3.addRow("ffmpeg 路徑", row)
        mv.addWidget(g3)
        mv.addStretch(1)
        tabs.addTab(media, "影音")

        # ---------------------------------------------------------------- AI 整合（MCP）
        tabs.addTab(self._build_ai_tab(), "AI 整合")

        about = QLabel(self._about_text())
        about.setStyleSheet("color: palette(placeholder-text);")
        about.setWordWrap(True)
        lay.addWidget(about)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setText("儲存")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ---------------------------------------------------------------- AI 整合（MCP）
    def _build_ai_tab(self) -> QWidget:
        s = self.settings
        page = QWidget()
        v = QVBoxLayout(page)
        intro = QLabel("讓支援 MCP（Model Context Protocol）的 AI 工具，例如 Codex、Cursor、VS Code，"
                       "透過本機連線使用 Divebird 下載檔案與影片。只有知道存取權杖的程式才能連線。")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.mcp_enabled = QCheckBox("啟用 MCP")
        self.mcp_enabled.setChecked(s.mcp_enabled)
        v.addWidget(self.mcp_enabled)

        conn = QGroupBox("連線資訊")
        fc = QFormLayout(conn)
        self.mcp_url = QLineEdit()
        self.mcp_url.setReadOnly(True)
        copy_url = QPushButton("複製")
        copy_url.clicked.connect(lambda: self._copy(self.mcp_url.text(), copy_url))
        row = QHBoxLayout()
        row.addWidget(self.mcp_url, 1)
        row.addWidget(copy_url)
        fc.addRow("網址", row)

        self.mcp_token = QLineEdit()
        self.mcp_token.setReadOnly(True)
        self.mcp_token.setEchoMode(QLineEdit.EchoMode.Password)
        show = QPushButton("顯示")
        show.setCheckable(True)
        show.toggled.connect(lambda on: (self.mcp_token.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password), show.setText("隱藏" if on else "顯示")))
        copy_token = QPushButton("複製")
        copy_token.clicked.connect(lambda: self._copy(self.mcp_token.text(), copy_token))
        regen = QPushButton("重新產生")
        regen.clicked.connect(self._regenerate_token)
        row = QHBoxLayout()
        row.addWidget(self.mcp_token, 1)
        for w in (show, copy_token, regen):
            row.addWidget(w)
        fc.addRow("存取權杖", row)

        row = QHBoxLayout()
        for label, kind in (("JSON（mcpServers）", "json"), ("Codex（config.toml）", "codex"),
                            ("VS Code（mcp.json）", "vscode")):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, k=kind, b=btn: self._copy(self._mcp_config(k), b))
            row.addWidget(btn)
        row.addStretch(1)
        fc.addRow("複製設定", row)
        hint = QLabel("AI 工具連線時要帶上 Authorization: Bearer <存取權杖>。權杖等同密碼，請勿分享。"
                      f"Divebird 需要保持執行（可縮小在{TRAY_NAME}），AI 工具才連得上。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text);")
        fc.addRow("", hint)
        v.addWidget(conn)

        perm = QGroupBox("AI 權限")
        fp = QFormLayout(perm)
        self.mcp_confirm = QComboBox()
        self.mcp_confirm.addItem("每次在 Divebird 跳出確認視窗（建議）", "always")
        self.mcp_confirm.addItem("不確認，直接下載並顯示通知", "never")
        self.mcp_confirm.setCurrentIndex(max(0, self.mcp_confirm.findData(s.mcp_confirm)))
        fp.addRow("AI 發起的下載", self.mcp_confirm)
        self.mcp_subdir = QCheckBox("允許指定下載資料夾底下的子資料夾")
        self.mcp_subdir.setChecked(s.mcp_allow_subdir)
        self.mcp_cookies = QCheckBox("允許傳入 Cookie 與登入資訊（Authorization）")
        self.mcp_cookies.setChecked(s.mcp_allow_cookies)
        self.mcp_private = QCheckBox("允許下載內網與本機位址")
        self.mcp_private.setChecked(s.mcp_allow_private)
        self.mcp_delete = QCheckBox("允許刪除已下載的檔案")
        self.mcp_delete.setChecked(s.mcp_allow_delete)
        self.mcp_share = QCheckBox("提供瀏覽器擴充功能偵測到的影音給 AI（AI 看不到 Cookie，下載時由擴充功能補上）")
        self.mcp_share.setChecked(s.mcp_share_browser_media)
        for box in (self.mcp_subdir, self.mcp_cookies, self.mcp_private, self.mcp_delete, self.mcp_share):
            fp.addRow("", box)
        v.addWidget(perm)
        v.addStretch(1)

        self._mcp_groups = (conn, perm)
        self.mcp_enabled.toggled.connect(self._mcp_toggled)
        self.port.valueChanged.connect(lambda _v: self._update_mcp_url())
        self._mcp_toggled(s.mcp_enabled)
        return page

    def _update_mcp_url(self) -> None:
        self.mcp_url.setText(f"http://127.0.0.1:{self.port.value()}/mcp")

    def _mcp_toggled(self, on: bool) -> None:
        for group in self._mcp_groups:
            group.setEnabled(on)      # 啟用 MCP 才能設定權限
        self._update_mcp_url()
        if on:
            self.mcp_token.setText(mcp_token.ensure())
        else:
            self.mcp_token.setText(mcp_token.load())
            self.mcp_token.setPlaceholderText("啟用後自動產生")

    def _regenerate_token(self) -> None:
        answer = QMessageBox.question(self, "重新產生存取權杖",
                                      "重新產生後，舊的權杖立即失效，已設定好的 AI 工具都要改用新的權杖。確定嗎？")
        if answer == QMessageBox.StandardButton.Yes:
            self.mcp_token.setText(mcp_token.reset())

    def _mcp_config(self, kind: str) -> str:
        url, auth = self.mcp_url.text(), f"Bearer {self.mcp_token.text()}"
        if kind == "codex":
            return (f'[mcp_servers.divebird]\nurl = "{url}"\n'
                    f'http_headers = {{ "Authorization" = "{auth}" }}\n')
        server = {"type": "http", "url": url, "headers": {"Authorization": auth}}
        return json.dumps({"servers" if kind == "vscode" else "mcpServers": {"divebird": server}},
                          ensure_ascii=False, indent=2)

    @staticmethod
    def _copy(text: str, button: QPushButton) -> None:
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        original = button.text()
        button.setText("已複製")
        QTimer.singleShot(1200, lambda: button.setText(original))

    # ---------------------------------------------------------------- 共用
    @staticmethod
    def _spin(lo, hi, val, suffix=""):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(val))
        if suffix:
            s.setSuffix(suffix)
        return s

    def _pick_dir(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "選擇資料夾", edit.text())
        if d:
            edit.setText(d)

    def _pick_ffmpeg(self):
        f, _ = QFileDialog.getOpenFileName(self, "選擇 ffmpeg 執行檔")
        if f:
            self.ffmpeg.setText(f)

    @staticmethod
    def _about_text() -> str:
        return f"Divebird {__version__}　·　{component_versions()}"

    def _save(self):
        s = self.settings
        s.download_dir = self.dir_edit.text().strip() or s.download_dir
        s.max_concurrent = self.concurrent.value()
        s.connections = self.connections.value()
        s.speed_limit_kbps = self.speed.value()
        s.port = self.port.value()
        s.show_dialog = self.show_dialog.isChecked()
        s.media_format = self.quality.currentData()
        s.merge_format = self.merge.currentText()
        s.ffmpeg_path = self.ffmpeg.text().strip()
        s.minimize_to_tray = self.tray.isChecked()
        s.notify_on_complete = self.notify.isChecked()
        s.mcp_enabled = self.mcp_enabled.isChecked()
        if s.mcp_enabled:
            mcp_token.ensure()
        s.mcp_confirm = self.mcp_confirm.currentData()
        s.mcp_allow_subdir = self.mcp_subdir.isChecked()
        s.mcp_allow_cookies = self.mcp_cookies.isChecked()
        s.mcp_allow_private = self.mcp_private.isChecked()
        s.mcp_allow_delete = self.mcp_delete.isChecked()
        s.mcp_share_browser_media = self.mcp_share.isChecked()
        s.save()
        if self.autostart.isChecked() != autostart.is_enabled():
            try:
                autostart.set_enabled(self.autostart.isChecked())
            except OSError:
                pass
        self.accept()


def component_versions() -> str:
    try:
        from yt_dlp.version import __version__ as ytv
    except Exception:  # noqa: BLE001
        ytv = "?"
    ok = lambda p: "✓ 內附" if p else "✗ 找不到"  # noqa: E731
    return f"yt-dlp {ytv}　·　ffmpeg {ok(ffmpeg_path())}　·　deno {ok(deno_path())}"


class _UpdateSignals(QObject):
    done = Signal(object)          # dict（GitHub 上的最新版本）或 str（錯誤訊息）


def _link(url: str, text: str) -> QLabel:
    label = QLabel(f'<a href="{url}">{text}</a>')
    label.setOpenExternalLinks(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    return label


class AboutDialog(QDialog):
    """關於：版本、作者、專案網址與授權，並可檢查 GitHub 上有沒有新版本。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("關於 Divebird")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 16)
        lay.setSpacing(16)

        head = QHBoxLayout()
        head.setSpacing(16)
        logo = QLabel()
        ratio = self.devicePixelRatioF()
        pix = QPixmap.fromImage(icons.render_svg(icons.app_svg_for(72), round(72 * ratio)))
        pix.setDevicePixelRatio(ratio)
        logo.setPixmap(pix)
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(4)
        title = QLabel("Divebird")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.9)
        font.setBold(True)
        title.setFont(font)
        names.addWidget(title)
        names.addWidget(QLabel("下載管理員　·　看準、俯衝、下載到手。"))
        names.addStretch(1)
        head.addLayout(names, 1)
        lay.addLayout(head)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(8)
        form.addRow("版本", QLabel(__version__))
        form.addRow("作者", _link(about.AUTHOR_URL, about.AUTHOR))
        form.addRow("專案網址", _link(about.REPO_URL, about.REPO_URL.removeprefix("https://")))
        form.addRow("授權", _link(about.LICENSE_URL, about.LICENSE_NAME))
        parts = QLabel(component_versions())
        parts.setWordWrap(True)
        form.addRow("內附元件", parts)
        lay.addLayout(form)

        buttons = QDialogButtonBox()
        self.check_btn = buttons.addButton("檢查更新", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        self.check_btn.clicked.connect(self.check_updates)
        lay.addWidget(buttons)

        self._sig = _UpdateSignals(self)
        self._sig.done.connect(self._update_checked)

    def check_updates(self) -> None:
        self.check_btn.setEnabled(False)
        self.check_btn.setText("檢查中…")
        sig = self._sig

        def work():
            try:
                result = about.latest_release()
            except about.UpdateCheckError as e:
                result = str(e)
            except Exception as e:  # noqa: BLE001
                result = str(e) or type(e).__name__
            try:
                sig.done.emit(result)
            except RuntimeError:        # 視窗已經關閉
                pass

        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, result) -> None:
        self.check_btn.setEnabled(True)
        self.check_btn.setText("檢查更新")
        if isinstance(result, str):
            QMessageBox.warning(self, "檢查更新", f"無法檢查更新：{result}")
        elif about.is_newer(result["version"]):
            if self._ask_update(result["version"]):
                QDesktopServices.openUrl(QUrl(result["url"]))
        else:
            QMessageBox.information(self, "檢查更新", f"已經是最新版本（{__version__}）。")

    def _ask_update(self, version: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "有新版本",
                          f"Divebird {version} 已經推出，目前使用的是 {__version__}。\n"
                          "要開啟 GitHub 的下載頁面更新嗎？", parent=self)
        yes = box.addButton("是", QMessageBox.ButtonRole.YesRole)
        box.addButton("否", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes
