"""內嵌 SVG 圖示（線條風格），依目前主題顏色即時著色。"""
from __future__ import annotations

import sys
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS = {
    "add": '<path d="M12 5v14M5 12h14"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
            '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "play": '<path d="M7 4.5v15l12-7.5z"/>',
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
    "play-all": '<path d="M3 5.5v13l8.5-6.5zM12.5 5.5v13l8.5-6.5z"/>',
    "pause-all": '<rect x="5" y="5" width="14" height="14" rx="2.5"/>',
    "trash": '<path d="M3 6h18M8 6V4.5A1.5 1.5 0 0 1 9.5 3h5A1.5 1.5 0 0 1 16 4.5V6M18.5 6l-.9 13.1A2 2 0 0 1 15.6 21H8.4'
             'a2 2 0 0 1-2-1.9L5.5 6M10 11v6M14 11v6"/>',
    "settings": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "folder": '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13'
              'A2.5 2.5 0 0 1 3 17.5z"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 15.4-6.4L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.4 6.4L3 16M3 21v-5h5"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5"/>',
    "open": '<path d="M14 3h7v7M10 14 21 3M18 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/>',
    "all": '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r=".6"/><circle cx="3.5" cy="12" r=".6"/>'
           '<circle cx="3.5" cy="18" r=".6"/>',
    "downloading": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v8M8.5 12l3.5 3.5 3.5-3.5"/>',
    "completed": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16.5 9.5"/>',
    "unfinished": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
    "video": '<rect x="2.5" y="6" width="13.5" height="12" rx="2"/><path d="m16 10.5 5.5-3v9l-5.5-3z"/>',
    "music": '<path d="M9 18V5.5l11-2V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    "archive": '<rect x="3" y="4" width="18" height="5" rx="1.2"/>'
               '<path d="M5 9v9.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V9M10 13h4"/>',
    "document": '<path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z"/>'
                '<path d="M14 3v5h5M8.5 13h7M8.5 17h5"/>',
    "program": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7.5 9.5 3 2.5-3 2.5M13 15h4"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/>'
             '<path d="m21 15-5-5L5 21"/>',
    "file": '<path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z"/>'
            '<path d="M14 3v5h5"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "quit": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
}

# Divebird 圖示：俯衝的燕子同時也是「下載」符號（箭桿＋箭頭＋底線）。
# 32px 以下改用簡化版，確保在工具列、系統匣仍是清楚的下載箭頭。
APP_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#0FA594"/>
<stop offset="1" stop-color="#0C6E99"/>
</linearGradient>
</defs>
<rect x="3" y="3" width="58" height="58" rx="14" fill="url(#bg)"/>
<g fill="#FFFFFF">
<path d="M29.6 37.6 C21 36.2 14 29.6 13 17.6 C16 21.8 22.8 25.3 30 25.8 Z"/>
<path d="M34.4 37.6 C43 36.2 50 29.6 51 17.6 C48 21.8 41.2 25.3 34 25.8 Z"/>
<path d="M28.6 38.6 L28 37 L28 26 C28 23 28.8 21 28.8 18.5 C28.8 15.5 28.1 12.6 27 9.8 C29.3 10.4 31 11.3 32 12.8 C33 11.3 34.7 10.4 37 9.8 C35.9 12.6 35.2 15.5 35.2 18.5 C35.2 21 36 23 36 26 L36 37 L35.4 38.6 Z"/>
<path d="M18.5 51.5 L30 51.5 L30 56.5 L18.5 56.5 C17.1 56.5 16 55.4 16 54 C16 52.6 17.1 51.5 18.5 51.5 Z"/>
<path d="M45.5 51.5 L34 51.5 L34 56.5 L45.5 56.5 C46.9 56.5 48 55.4 48 54 C48 52.6 46.9 51.5 45.5 51.5 Z"/>
</g>
<path fill="#FCD34D" d="M28.1 37.6 C28.9 36.5 30.3 35.9 32 35.9 C33.7 35.9 35.1 36.5 35.9 37.6 C36.4 38.3 36.7 39.1 36.7 40 C36.7 41.3 35.9 42.1 35.1 43 L32 48.5 L28.9 43 C28.1 42.1 27.3 41.3 27.3 40 C27.3 39.1 27.6 38.3 28.1 37.6 Z"/>
</svg>"""

APP_SVG_SMALL = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#0FA594"/>
<stop offset="1" stop-color="#0C6E99"/>
</linearGradient>
</defs>
<rect x="3" y="3" width="58" height="58" rx="14" fill="url(#bg)"/>
<g fill="#FFFFFF">
<path d="M29.6 37.6 C21 36.2 14 29.6 13 17.6 C16 21.8 22.8 25.3 30 25.8 Z"/>
<path d="M34.4 37.6 C43 36.2 50 29.6 51 17.6 C48 21.8 41.2 25.3 34 25.8 Z"/>
<path d="M28 40 L28 21.5 C28 16.8 27.8 13.4 27 9.8 C29.3 10.4 31 11.3 32 12.8 C33 11.3 34.7 10.4 37 9.8 C36.2 13.4 36 16.8 36 21.5 L36 40 Z"/>
<path d="M28 35 C27.6 36.6 27.3 38.4 27.3 40 C27.3 41.3 28.1 42.1 28.9 43 L32 48.5 L35.1 43 C35.9 42.1 36.7 41.3 36.7 40 C36.7 38.4 36.4 36.6 36 35 Z"/>
<rect x="16" y="52" width="32" height="4" rx="2"/>
</g>
</svg>"""

SMALL_MAX = 32


def app_svg_for(size: int) -> bytes:
    return (APP_SVG_SMALL if size <= SMALL_MAX else APP_SVG).encode()


def _svg(name: str, color: str, filled: bool = False) -> bytes:
    body = _PATHS[name]
    fill = color if filled else "none"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{fill}" stroke="{color}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>').encode()


def render_svg(data: bytes, size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(data)).render(p, QRectF(0, 0, size, size))
    p.end()
    return img


def _icon_from_svg(data: bytes) -> QIcon:
    icon = QIcon()
    for s in (16, 20, 24, 32, 48, 64):
        icon.addPixmap(QPixmap.fromImage(render_svg(data, s)))
    return icon


_FILLED = {"play", "play-all"}


@lru_cache(maxsize=256)
def icon(name: str, color: str = "#475569") -> QIcon:
    return _icon_from_svg(_svg(name, color, filled=name in _FILLED))


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    icon_ = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon_.addPixmap(QPixmap.fromImage(render_svg(app_svg_for(s), s)))
    return icon_


# macOS 選單列圖示：只有燕子＋箭頭的黑色輪廓，設為「範本圖片」（isMask），系統會依淺色／深色選單列自動著色
TRAY_SVG_MAC = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="7 8 50 50">
<g fill="#000000">
<path d="M29.6 37.6 C21 36.2 14 29.6 13 17.6 C16 21.8 22.8 25.3 30 25.8 Z"/>
<path d="M34.4 37.6 C43 36.2 50 29.6 51 17.6 C48 21.8 41.2 25.3 34 25.8 Z"/>
<path d="M28 40 L28 21.5 C28 16.8 27.8 13.4 27 9.8 C29.3 10.4 31 11.3 32 12.8 C33 11.3 34.7 10.4 37 9.8 C36.2 13.4 36 16.8 36 21.5 L36 40 Z"/>
<path d="M28 35 C27.6 36.6 27.3 38.4 27.3 40 C27.3 41.3 28.1 42.1 28.9 43 L32 48.5 L35.1 43 C35.9 42.1 36.7 41.3 36.7 40 C36.7 38.4 36.4 36.6 36 35 Z"/>
<rect x="16" y="52" width="32" height="4" rx="2"/>
</g>
</svg>"""


@lru_cache(maxsize=1)
def tray_icon() -> QIcon:
    """系統匣／選單列圖示：macOS 用單色範本圖片（與其他選單列圖示一致），其他平台用彩色程式圖示。"""
    if sys.platform != "darwin":
        return app_icon()
    icon_ = QIcon()
    for s in (16, 18, 32, 36, 64):
        icon_.addPixmap(QPixmap.fromImage(render_svg(TRAY_SVG_MAC.encode(), s)))
    icon_.setIsMask(True)
    return icon_


@lru_cache(maxsize=1)
def check_mark_path() -> str:
    """核取方塊的白色勾勾圖（樣式表的 image: url() 需要實體檔案）。"""
    import tempfile
    from pathlib import Path

    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M3.5 8.4l2.9 2.9 6.1-6.6" '
           b'fill="none" stroke="#ffffff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    path = Path(tempfile.gettempdir()) / "divebird-check.png"
    render_svg(svg, 32).save(str(path))
    return path.as_posix()


def themed(name: str, dark: bool) -> QIcon:
    return icon(name, "#cbd5e1" if dark else "#475569")


def accent(name: str, color: QColor | str) -> QIcon:
    return icon(name, QColor(color).name())
