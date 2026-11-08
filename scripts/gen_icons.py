"""由內嵌 SVG 產生擴充功能圖示（PNG）與桌面程式圖示（Windows .ico、macOS .icns、Linux .png）。
32px 以下使用簡化版圖示（見 divebird.gui.icons.app_svg_for）。"""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from divebird.gui.icons import app_svg_for, render_svg  # noqa: E402

# macOS .icns 的項目（類型代碼、像素），每種都存 PNG。不使用 icp4／icp5／icp6：Finder 等系統介面
# （IconServices）不把它們當成 PNG 解碼，1x 螢幕上的 16／32 px 圖示會變成雜訊；這些尺寸由系統從 ic11／ic12 縮小
ICNS_TYPES = (("ic11", 32), ("ic12", 64), ("ic07", 128), ("ic13", 256), ("ic08", 256), ("ic14", 512),
              ("ic09", 512), ("ic10", 1024))


def _png(img) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def mac_svg_for(size: int) -> bytes:
    """macOS 圖示的圖案約佔畫布 80%（Apple 的圖示格線，四周留白），否則在 Dock 會比其他 App 大一圈。
    原圖的圓角方塊佔 64 單位中的 58，把 viewBox 放大到 58 / 0.805 單位、置中即可。"""
    side = 58 / 0.805
    origin = 32 - side / 2
    return app_svg_for(size).replace(b'viewBox="0 0 64 64"',
                                     f'viewBox="{origin:.3f} {origin:.3f} {side:.3f} {side:.3f}"'.encode(), 1)


def write_icns(path: Path) -> None:
    body = b""
    for kind, s in ICNS_TYPES:
        data = _png(render_svg(mac_svg_for(s), s))
        body += kind.encode() + struct.pack(">I", 8 + len(data)) + data
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841
    out = ROOT / "extension" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for s in (16, 32, 48, 128):
        render_svg(app_svg_for(s), s).save(str(out / f"icon{s}.png"))
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    render_svg(app_svg_for(256), 256).save(str(assets / "divebird.png"))
    # Windows .ico（多尺寸）：Qt 的 ico 外掛只寫入單一尺寸，這裡手動組合
    sizes = (16, 24, 32, 48, 64, 128, 256)
    blobs = []
    for s in sizes:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        render_svg(app_svg_for(s), s).save(buf, "PNG")
        blobs.append(bytes(buf.data()))
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, b in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(b), offset)
        offset += len(b)
    (assets / "divebird.ico").write_bytes(header + entries + b"".join(blobs))
    write_icns(assets / "divebird.icns")
    print("icons generated")


if __name__ == "__main__":
    main()
