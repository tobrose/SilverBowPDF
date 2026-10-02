"""Turn a picture of a handwritten signature into a SignatureImage.

Most signature pictures are a scan or a phone photo: dark ink on white
paper. Laid on a page as they are, that's a white rectangle with a
signature in it. So the paper is made transparent -- near-white pixels
fade to clear, ink keeps its colour -- and the picture is trimmed to the
ink and scaled down to a sensible size. Pictures that already have
transparency (a PNG made for this) keep it.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from .annots import SignatureImage

MAX_WIDTH = 1200        # pixels: sharp at print size, small in the file
PAPER_FROM = 200        # lightness where paper starts fading out...
PAPER_CLEAR = 238       # ...and where it's fully transparent
TRIM_ALPHA = 12         # pixels fainter than this don't count as ink when trimming


class SignatureImageError(ValueError):
    pass


def load_signature_image(path: str | Path, clear_paper: bool = True) -> SignatureImage:
    image = QImage(str(path))
    if image.isNull():
        raise SignatureImageError(f"{Path(path).name} isn't an image SilverbowPDF can read "
                                  "(use PNG or JPEG).")
    return from_qimage(image, clear_paper and not image.hasAlphaChannel())


def from_qimage(image: QImage, clear_paper: bool = True) -> SignatureImage:
    if image.width() > MAX_WIDTH:
        image = image.scaledToWidth(MAX_WIDTH, Qt.SmoothTransformation)
    image = image.convertToFormat(QImage.Format_RGBA8888)  # straight alpha, byte order fixed
    w, h = image.width(), image.height()
    stride = image.bytesPerLine()
    raw = bytes(image.constBits())
    rgba = b"".join(raw[y * stride:y * stride + w * 4] for y in range(h))
    alpha = bytearray(rgba[3::4])
    if clear_paper:
        span = PAPER_CLEAR - PAPER_FROM
        r, g, b = rgba[0::4], rgba[1::4], rgba[2::4]
        for i in range(w * h):
            light = min(r[i], g[i], b[i])  # the darkest channel: ink in any colour counts
            if light >= PAPER_CLEAR:
                alpha[i] = 0
            elif light > PAPER_FROM:
                alpha[i] = alpha[i] * (PAPER_CLEAR - light) // span
    # Trim to the ink, with a small margin, so the box hugs the signature.
    rows = [y for y in range(h) if max(alpha[y * w:(y + 1) * w]) > TRIM_ALPHA]
    if not rows:
        raise SignatureImageError("That picture looks blank -- no ink was found in it.")
    cols = [x for x in range(w) if max(alpha[x::w]) > TRIM_ALPHA]
    margin = max(2, min(w, h) // 50)
    top, bottom = max(0, rows[0] - margin), min(h, rows[-1] + 1 + margin)
    left, right = max(0, cols[0] - margin), min(w, cols[-1] + 1 + margin)
    tw, th = right - left, bottom - top
    cropped = b"".join(rgba[(y * w + left) * 4:(y * w + right) * 4] for y in range(top, bottom))
    rgb = bytearray(tw * th * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = cropped[0::4], cropped[1::4], cropped[2::4]
    a = b"".join(bytes(alpha[y * w + left:y * w + right]) for y in range(top, bottom))
    return SignatureImage(tw, th, zlib.compress(bytes(rgb), 9), zlib.compress(a, 9))


def to_qimage(image: SignatureImage) -> QImage:
    """For previews in dialogs."""
    rgb, alpha = zlib.decompress(image.rgb), zlib.decompress(image.alpha)
    rgba = bytearray(image.width * image.height * 4)
    rgba[0::4], rgba[1::4], rgba[2::4] = rgb[0::3], rgb[1::3], rgb[2::3]
    rgba[3::4] = alpha
    return QImage(bytes(rgba), image.width, image.height, image.width * 4,
                  QImage.Format_RGBA8888).copy()
