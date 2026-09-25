"""Turn pdfium bitmaps into QImages, with a bounded cache.

The format mapping here is the one part of the app that depends on pdfium's
internal buffer layout, so it inspects the bitmap's reported mode at runtime
rather than assuming. pdfium picks its format opportunistically -- a page with
no transparency comes back as 24-bit BGR unless you ask otherwise -- so
hardcoding one format is a bug waiting for the wrong PDF.
"""

from __future__ import annotations

import functools
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtGui import QImage

from .annotpdf import overlay_pdf
from .document import PdfDocument

_LITTLE_ENDIAN = sys.byteorder == "little"

# pdfium buffer mode -> QImage format. QImage's ARGB32/RGB32 are uint32 formats,
# so on a little-endian machine their in-memory byte order is B,G,R,A -- which
# is exactly what pdfium hands back. The 8888 formats are byte-order defined and
# therefore endian-independent, so they are the safe fallback.
_MODE_TO_FORMAT = {
    "BGRA": QImage.Format_ARGB32 if _LITTLE_ENDIAN else QImage.Format_RGBA8888,
    "BGRX": QImage.Format_RGB32 if _LITTLE_ENDIAN else QImage.Format_RGBX8888,
    "BGR": QImage.Format_BGR888,
    "RGBA": QImage.Format_RGBA8888,
    "RGBX": QImage.Format_RGBX8888,
    "RGB": QImage.Format_RGB888,
    "L": QImage.Format_Grayscale8,
    "Gray": QImage.Format_Grayscale8,
}


class UnsupportedBitmapFormat(RuntimeError):
    pass


def bitmap_to_qimage(bitmap) -> QImage:
    """Copy a pdfium bitmap into an owned QImage.

    The copy is deliberate: the pdfium bitmap is closed immediately after
    rendering, and a QImage constructed over a foreign buffer does not take
    ownership. Skipping the copy here buys a dangling pointer and a crash that
    only shows up under memory pressure.
    """
    mode = getattr(bitmap, "mode", None)
    fmt = _MODE_TO_FORMAT.get(mode)
    if fmt is None:
        raise UnsupportedBitmapFormat(
            f"pdfium returned an unhandled bitmap mode: {mode!r}. "
            f"Add it to _MODE_TO_FORMAT in rendering.py."
        )

    data = bytes(bitmap.buffer)
    image = QImage(data, bitmap.width, bitmap.height, bitmap.stride, fmt)
    # .copy() detaches from `data` before it goes out of scope.
    return image.copy()


class PageRenderer:
    """Renders pages on demand and keeps the most recent results.

    Rendering is synchronous. On a text page at normal zoom that is a couple of
    milliseconds and nobody notices; on a 600 DPI scan at 400% it will hitch the
    UI for a beat. Moving this onto a QThreadPool is the next step, and the
    interface is shaped so that swap doesn't touch the widgets.
    """

    def __init__(self, document: PdfDocument, max_pages: int = 12):
        self.document = document
        self.max_pages = max_pages
        self._cache: "OrderedDict[tuple, QImage]" = OrderedDict()

    def _key(self, index: int, scale: float, rotation: int) -> tuple:
        # Quantise the scale so that float noise from fit calculations doesn't
        # produce a cache miss on every repaint. The page's annotation edits
        # are part of the key, so an edit is never served a stale image.
        edits = self.document.annots.get(self.document.page_key(index))
        return (index, int(round(scale * 1000)), rotation % 360, edits)

    def _overlay(self, index: int) -> Optional[bytes]:
        """The page's edited annotations as a one-page PDF, or None if the
        page has no annotation edits and renders straight from the file."""
        doc = self.document
        edits = doc.annots.get(doc.page_key(index))
        if edits is None:
            return None
        ref = doc.pages[index]
        source = doc.sources[ref.file]
        media, crop, rotate = doc.page_geometry(index)
        stat = Path(source.path).stat()
        return _cached_overlay(str(source.path), stat.st_mtime_ns, stat.st_size,
                               source.password, ref.source, edits, media, crop, rotate)

    def image(self, index: int, scale: float, view_rotation: int = 0) -> QImage:
        key = self._key(index, scale, view_rotation)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        bitmap = self.document.render_bitmap(index, scale, view_rotation, self._overlay(index))
        try:
            image = bitmap_to_qimage(bitmap)
        finally:
            bitmap.close()

        # Cross-check pdfium against our own geometry. If these ever disagree,
        # every coordinate in the app is wrong and we want to know immediately.
        expected = self.document.transform(index, scale, view_rotation).bitmap_size
        if (image.width(), image.height()) != expected:
            raise RuntimeError(
                f"page {index}: pdfium rendered {image.width()}x{image.height()} "
                f"but PageTransform predicted {expected[0]}x{expected[1]}. "
                f"Coordinate mapping cannot be trusted."
            )

        self._cache[key] = image
        while len(self._cache) > self.max_pages:
            self._cache.popitem(last=False)
        return image

    def cached(self, index: int, scale: float, view_rotation: int = 0) -> Optional[QImage]:
        """Return the image only if it is already rendered. Never blocks."""
        return self._cache.get(self._key(index, scale, view_rotation))

    def clear(self) -> None:
        self._cache.clear()


@functools.lru_cache(maxsize=32)
def _cached_overlay(path, mtime_ns, size, password, page_index, edits, media, crop, rotate):
    """Shared by every renderer (page view, thumbnails), so one edit builds
    its overlay once. mtime and size are in the key so an overlay built from
    a file's old contents is never reused after that file is saved over."""
    return overlay_pdf(Path(path), password, page_index, edits, media, crop, rotate)
