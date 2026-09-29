"""Make the app icon from packaging/icon-source.png.

    python scripts/make_icon.py

Writes packaging/icon.ico (exe, shortcuts, installer) and pdfapp/icon.png
(the window/taskbar icon at runtime). Both are committed; rerun this only
when the source picture changes.

The source is a red rounded square on a white background with a soft grey
shadow. Only the red square is kept: each row is cut to the span between its
leftmost and rightmost red pixel, so the corners -- whatever their curve --
come out transparent instead of white. Qt only; no new dependencies.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "packaging" / "icon-source.png"
ICO = ROOT / "packaging" / "icon.ico"
PNG = ROOT / "pdfapp" / "icon.png"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)  # what Windows asks for at 100-200% scaling


def _is_red(r: int, g: int, b: int) -> bool:
    return r > 140 and r - g > 70 and r - b > 70


def cut_out(source: QImage) -> QImage:
    """The red rounded square alone, centred on a transparent square canvas."""
    img = source.convertToFormat(QImage.Format_RGBA8888)
    w, h, stride = img.width(), img.height(), img.bytesPerLine()
    data = bytes(img.constBits())
    spans = {}  # row -> (first red x, last red x)
    for y in range(h):
        row = data[y * stride:y * stride + w * 4]
        xs = [x for x in range(0, w, 1) if _is_red(row[4 * x], row[4 * x + 1], row[4 * x + 2])]
        if len(xs) > w // 20:  # a real row of the square, not a stray reddish pixel
            spans[y] = (xs[0], xs[-1])
    if not spans:
        raise SystemExit(f"no red square found in {SOURCE.name}")
    top, bottom = min(spans), max(spans)
    left = min(a for a, _ in spans.values())
    right = max(b for _, b in spans.values())
    side = max(right - left + 1, bottom - top + 1)
    ox = (side - (right - left + 1)) // 2
    oy = (side - (bottom - top + 1)) // 2

    out = bytearray(side * side * 4)  # transparent
    for y, (a, b) in spans.items():
        src = y * stride + a * 4
        dst = ((y - top + oy) * side + (a - left + ox)) * 4
        run = data[src:src + (b - a + 1) * 4]
        out[dst:dst + len(run)] = run
        for i in range(dst + 3, dst + len(run), 4):
            out[i] = 255  # the source is opaque; make sure the cut-out is too
    return QImage(bytes(out), side, side, side * 4, QImage.Format_RGBA8888).copy()


def scaled(img: QImage, size: int) -> QImage:
    """Halve repeatedly, then one smooth step: a single 1000px -> 16px jump
    skips most of the pixels and comes out jagged."""
    while img.width() >= size * 2:
        img = img.scaled(img.width() // 2, img.height() // 2,
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return img.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _png_bytes(img: QImage) -> bytes:
    buf = QByteArray()
    device = QBuffer(buf)
    device.open(QIODevice.WriteOnly)
    img.save(device, "PNG")
    return bytes(buf)


def _dib_bytes(img: QImage) -> bytes:
    """Classic 32-bit BMP icon entry. Sizes below 256 use this rather than
    PNG: every Windows component reads it, some older ones don't read PNG."""
    img = img.convertToFormat(QImage.Format_ARGB32)  # BGRA in memory on Windows
    size = img.width()
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = [bytes(img.constScanLine(y))[:size * 4] for y in range(size)]
    pixels = b"".join(reversed(rows))  # bottom-up
    mask_row = ((size + 31) // 32) * 4
    return header + pixels + bytes(mask_row * size)  # AND mask unused: alpha does it


def write_ico(master: QImage, path: Path) -> None:
    entries = []
    for size in ICO_SIZES:
        img = scaled(master, size)
        entries.append((size, _png_bytes(img) if size >= 256 else _dib_bytes(img)))
    offset = 6 + 16 * len(entries)
    head = struct.pack("<HHH", 0, 1, len(entries))
    table, blobs = b"", b""
    for size, blob in entries:
        dim = 0 if size >= 256 else size  # 0 means 256
        table += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    path.write_bytes(head + table + blobs)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    source = QImage(str(SOURCE))
    if source.isNull():
        raise SystemExit(f"can't read {SOURCE}")
    master = cut_out(source)
    write_ico(master, ICO)
    scaled(master, 256).save(str(PNG), "PNG")
    print(f"{SOURCE.name} {source.width()}x{source.height()} -> red square "
          f"{master.width()}px -> {ICO.relative_to(ROOT)} ({len(ICO_SIZES)} sizes), "
          f"{PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
