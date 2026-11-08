"""重新產生 README 的截圖：主視窗、「設定 → AI 整合」、「關於」（淺色 / 深色主題各一張）。

「關於」與設定頁底部顯示版本號，發布新版前要重新產生：

    .venv/Scripts/python.exe scripts/readme_screenshots.py      （Linux／macOS：.venv/bin/python）

視窗只在記憶體裡繪製（WA_DontShowOnScreen），不會出現在桌面上，也不建立系統匣圖示；
使用臨時設定資料夾與非預設埠，不影響使用者的資料。輸出到 docs/images/。
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"


def shoot(scheme: str) -> None:
    os.environ["APPDATA"] = tempfile.mkdtemp(prefix="divebird-shot-")
    os.environ["XDG_CONFIG_HOME"] = os.environ["APPDATA"]
    if sys.platform == "darwin":    # macOS 的設定資料夾在家目錄底下（~/Library/Application Support）
        os.environ["HOME"] = os.environ["APPDATA"]

    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QTabWidget

    QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: False)     # 不在系統匣放圖示

    from divebird.config import Settings
    from divebird.gui import app as gapp
    from divebird.gui.dialogs import AboutDialog, SettingsDialog
    from divebird.models import Kind, Status, Task

    app = QApplication([])
    app.styleHints().setColorScheme(Qt.ColorScheme.Light if scheme == "light" else Qt.ColorScheme.Dark)
    app.setStyle("Fusion")
    downloads = r"D:\Downloads"
    ctl = gapp.Controller(app, Settings(download_dir=downloads, port=17996, show_dialog=False))
    app.setStyleSheet(gapp.stylesheet(ctl.dark))

    def offscreen(widget):
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.show()
        app.processEvents()

    def beautify(img: QImage, radius: float = 22, margin: int = 56) -> QImage:
        """圓角＋柔和陰影，放在透明背景上（GitHub 淺色 / 深色頁面都好看）。"""
        img = QImage(img)
        img.setDevicePixelRatio(1.0)
        w, h = img.width(), img.height()
        out = QImage(w + 2 * margin, h + 2 * margin, QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(margin, 0, -2):
            p.setBrush(QColor(0, 0, 0, int(34 * (1 - i / margin) ** 2.2)))
            p.drawRoundedRect(QRectF(margin - i * 0.6, margin - i * 0.6 + i * 0.25, w + i * 1.2, h + i * 1.2),
                              radius + i * 0.6, radius + i * 0.6)
        path = QPainterPath()
        path.addRoundedRect(QRectF(margin, margin, w, h), radius, radius)
        p.setClipPath(path)
        p.drawImage(margin, margin, img)
        p.end()
        return out

    def scaled(img: QImage, width: int) -> QImage:
        img = QImage(img)
        img.setDevicePixelRatio(1.0)
        return img.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation) if img.width() > width else img

    now = time.time()
    gb, mb = 1024 ** 3, 1024 ** 2
    demo = [
        Task(url="https://example.com/ubuntu-24.04.3-desktop-amd64.iso", filename="ubuntu-24.04.3-desktop-amd64.iso",
             status=Status.DOWNLOADING, total=int(6.1 * gb), downloaded=int(2.48 * gb), speed=24.6 * mb,
             active_connections=8, created_at=now - 120),
        Task(url="https://example.com/watch/ocean-dive", kind=Kind.MEDIA, filename="Ocean Dive 4K 示範影片.mp4",
             status=Status.DOWNLOADING, total=int(812 * mb), downloaded=int(431 * mb), speed=9.8 * mb,
             active_connections=8, created_at=now - 60),
        Task(url="https://example.com/course/lesson-3.m3u8", kind=Kind.MEDIA, filename="線上課程 第 3 講.mp4",
             status=Status.PROCESSING, total=int(356 * mb), downloaded=int(356 * mb), created_at=now - 400),
        Task(url="https://example.com/LibreOffice_25.8_Win_x86-64.msi", filename="LibreOffice_25.8_Win_x86-64.msi",
             status=Status.COMPLETED, total=int(342 * mb), downloaded=int(342 * mb), created_at=now - 3600),
        Task(url="https://example.com/podcast-episode-42.mp3", filename="podcast-episode-42.mp3",
             status=Status.QUEUED, created_at=now - 200),
        Task(url="https://example.com/dataset-2026.zip", filename="dataset-2026.zip", status=Status.PAUSED,
             total=int(1.4 * gb), downloaded=int(0.42 * gb), created_at=now - 7200),
        Task(url="https://example.com/files/annual-report-2025.pdf", filename="annual-report-2025.pdf",
             status=Status.ERROR, error="HTTP 403 拒絕存取", created_at=now - 9000),
        Task(url="https://example.com/blender-4.5-linux-x64.tar.xz", filename="blender-4.5-linux-x64.tar.xz",
             status=Status.COMPLETED, total=int(388 * mb), downloaded=int(388 * mb), created_at=now - 86400),
    ]
    for t in demo:
        t.save_dir = downloads
        ctl.manager.tasks[t.id] = t
        ctl.bridge.task_added(t)

    w = ctl.window
    ctl.settings.port = 17890          # 畫面上顯示預設埠（實際監聽的是臨時埠）
    ctl.settings.mcp_enabled = True
    w.update_api_status()
    w.resize(1240, 540)
    offscreen(w)
    w.table.selectRow(0)

    def snap():
        w._tick()
        app.processEvents()
        beautify(scaled(w.grab().toImage(), 2200)).save(str(OUT / f"main-{scheme}.png"))
        s = SettingsDialog(ctl.settings)
        s.findChild(QTabWidget).setCurrentIndex(3)       # AI 整合
        offscreen(s)
        beautify(scaled(s.grab().toImage(), 1500)).save(str(OUT / f"settings-ai-{scheme}.png"))
        s.close()
        a = AboutDialog()
        offscreen(a)
        beautify(scaled(a.grab().toImage(), 1500)).save(str(OUT / f"about-{scheme}.png"))
        a.close()
        for t in demo:
            t.status = Status.PAUSED
        ctl.manager.tasks.clear()
        ctl.quit()

    QTimer.singleShot(900, snap)
    app.exec()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        shoot(sys.argv[1])
    else:   # 每個主題各用一個行程（QApplication 只能建立一次）
        for scheme in ("light", "dark"):
            subprocess.run([sys.executable, __file__, scheme], check=True)
        print("已更新", OUT)
