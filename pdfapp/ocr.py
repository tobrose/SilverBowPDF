"""Recognise text on scanned pages with Windows' own OCR (Windows.Media.Ocr).

Nothing to ship but a ~3MB bridge (the winrt packages): the engine and its
language data come with Windows 10/11. Which languages it reads depends on
the language packs installed; English is there on an English install.

A page is rendered by pdfium as it's shown (edit rotation included, so a
page turned upright reads upright), recognised, and each word's pixel box
mapped back to PDF space through the page's PageTransform -- the same
geometry everything else in the app uses. The result is ocrlayer.OcrWords:
the text layer an OcrEdits entry holds until the file is saved.

No Qt widgets or painting -- but see engine() for why it loads QtCore
before winrt. Imported lazily: winrt is only needed when someone actually
asks for OCR, and a failure to load it must say so, not break startup.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from .document import PdfDocument
from .ocrlayer import OcrWord, word_matrix

OCR_DPI = 300          # what Windows OCR reads best; small print needs it
_engine = None


class OcrUnavailable(RuntimeError):
    pass


def engine():
    """The Windows OCR engine for the user's languages. Raises
    OcrUnavailable (saying why) rather than returning None."""
    global _engine
    if _engine is None:
        # Load order matters, and it's not ours to leave to chance: winrt and
        # PySide6 each ship their own msvcp140.dll, and loading winrt's first
        # makes importing Qt afterwards an access violation (measured). Qt's
        # first, then winrt, works. The app always has Qt up by now; this
        # makes the self-test, tests and any future caller safe too.
        import PySide6.QtCore  # noqa: F401
        try:
            from winrt.windows.media.ocr import OcrEngine
        except Exception as exc:
            raise OcrUnavailable(f"Windows OCR couldn't be loaded ({exc}).") from exc
        _engine = OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            raise OcrUnavailable(
                "Windows has no OCR language installed for this user. Add one in "
                "Settings > Time & language > Language & region (the language's "
                "optional features include OCR).")
    return _engine


def language() -> str:
    return engine().recognizer_language.display_name


def needs_ocr(doc: PdfDocument, index: int) -> bool:
    """A page with no text at all -- a scan. Pages that have any text are
    left alone: a second, invisible copy of it would double every search."""
    return not doc.has_text(index)


def recognize_page(doc: PdfDocument, index: int) -> Tuple[OcrWord, ...]:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    ocr = engine()
    # 300 DPI, but never past the engine's limit (a poster-sized page).
    longest = max(doc.transform(index, 1.0).display_size_pt)
    scale = min(OCR_DPI / 72.0, (OcrEngine.max_image_dimension - 1) / longest)
    transform = doc.transform(index, scale)
    bitmap = doc.render_content(index, scale)
    try:
        if bitmap.mode not in ("BGRA", "BGRX"):
            raise RuntimeError(f"unexpected bitmap format {bitmap.mode}")
        w, h, stride = bitmap.width, bitmap.height, bitmap.stride
        data = bytes(bitmap.buffer)
    finally:
        bitmap.close()
    if stride != w * 4:  # SoftwareBitmap wants packed rows
        data = b"".join(data[y * stride:y * stride + w * 4] for y in range(h))
    writer = DataWriter()
    writer.write_bytes(data)
    image = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)

    async def run():
        return await ocr.recognize_async(image)

    result = asyncio.run(run())
    return tuple(_words(result, transform))


def _words(result, transform) -> List[OcrWord]:
    """Pixel boxes -> text matrices. Every word on a line shares the line's
    top and bottom (so selecting across a line is one even band), and each
    word but the last carries a space stretched over the gap to the next,
    so a search for "two words" matches across it."""
    pdf = transform.bitmap_to_pdf
    words: List[OcrWord] = []
    for line in result.lines:
        boxes = [(wd.text, wd.bounding_rect) for wd in line.words if wd.text]
        if not boxes:
            continue
        top = min(r.y for _, r in boxes)
        bottom = max(r.y + r.height for _, r in boxes)
        for i, (text, r) in enumerate(boxes):
            left = r.x
            if i + 1 < len(boxes):
                text += " "
                right = boxes[i + 1][1].x  # the space runs up to the next word
            else:
                right = r.x + r.width
            matrix = word_matrix(pdf(left, bottom), pdf(right, bottom), pdf(left, top), len(text))
            words.append(OcrWord(text, matrix))
    return words


def recognize(doc: PdfDocument, pages: List[int],
              progress=None) -> Tuple[dict, List[Tuple[int, str]]]:
    """OCR each page in `pages` (displayed indices). progress(n) before each;
    False cancels. Returns ({page key: words}, [(index, error)]). One page
    failing doesn't stop the rest; the caller reports it."""
    found, failures = {}, []
    for n, index in enumerate(pages):
        if progress is not None and not progress(n):
            break
        try:
            found[doc.page_key(index)] = recognize_page(doc, index)
        except OcrUnavailable:
            raise
        except Exception as exc:
            failures.append((index, str(exc)))
    return found, failures
