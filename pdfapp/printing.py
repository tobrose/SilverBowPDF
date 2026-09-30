"""File > Print: each page rendered by pdfium and drawn onto a sheet.

What prints is what the screen shows -- page order and rotation, marks,
typed text, filled-in fields, saved or not -- minus the tint that marks
form fields on screen (PageRenderer.print_image).

Pages are fitted to the printable area, centred, and turned a quarter
when that makes them bigger (a landscape page on portrait paper), as
Acrobat's "Fit" does. Rendering stops at PRINT_DPI: sharp for text and
scans, and each sheet stays a size a redirected (remote-session) printer can spool.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtPrintSupport import QPrinter

from .rendering import PageRenderer

PRINT_DPI = 300


class PrintError(RuntimeError):
    pass


def place_on_sheet(page_w: float, page_h: float, area_w: float, area_h: float
                   ) -> Tuple[bool, float, QRectF]:
    """Fit a page (points) into the printable area (device px).

    Returns (turned, px_per_point, target) -- target is where the page lands
    on the sheet, in sheet orientation, so turned pages have their width and
    height swapped there."""
    straight = min(area_w / page_w, area_h / page_h)
    turned_fit = min(area_w / page_h, area_h / page_w)
    turned = turned_fit > straight * 1.02  # only when clearly bigger: no flip-flopping on squares
    s = turned_fit if turned else straight
    w, h = (page_h * s, page_w * s) if turned else (page_w * s, page_h * s)
    return turned, s, QRectF((area_w - w) / 2, (area_h - h) / 2, w, h)


def print_pages(renderer: PageRenderer, printer: QPrinter, pages: Sequence[int],
                progress: Optional[Callable[[int], bool]] = None) -> List[Tuple[int, str]]:
    """Print `pages` (displayed indices, in order). progress(n) is called
    before each sheet; returning False cancels the job.

    Returns the pages that failed to render, as (index, error). Like the
    screen, a page that fails doesn't stop the others -- its sheet says what
    went wrong instead, and the caller tells the user.
    """
    doc = renderer.document
    painter = QPainter()
    if not painter.begin(printer):
        raise PrintError("The printer could not be started. Check that it's available.")
    failures: List[Tuple[int, str]] = []
    try:
        area = painter.viewport()  # the printable area, in device pixels
        device_dpi = printer.resolution()
        for n, index in enumerate(pages):
            if progress is not None and not progress(n):
                printer.abort()
                break
            if n:
                printer.newPage()
            page_w, page_h = doc.transform(index).display_size_pt
            turned, s, target = place_on_sheet(page_w, page_h, area.width(), area.height())
            try:
                # s is device px per point; no point rendering finer than the
                # sheet will show, nor finer than PRINT_DPI.
                image = renderer.print_image(index, min(s, PRINT_DPI / 72.0))
            except Exception as exc:  # isolate, display, don't hide (rule 7)
                failures.append((index, str(exc)))
                painter.setPen(QColor("#b00020"))
                painter.drawText(target.adjusted(device_dpi / 2, device_dpi / 2, 0, 0),
                                 Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap,
                                 f"Page {index + 1} could not be printed:\n{exc}")
                continue
            painter.save()
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            if turned:
                # Turn against the page's own rotation: a portrait page shown
                # sideways by /Rotate 90 comes out upright again, not upside
                # down. Plain landscape pages go counter-clockwise, as Acrobat.
                painter.translate(target.center())
                painter.rotate(90 if doc.page_rotation(index) == 270 else -90)
                w, h = target.height(), target.width()
                painter.drawImage(QRectF(QPointF(-w / 2, -h / 2), QPointF(w / 2, h / 2)), image)
            else:
                painter.drawImage(target, image)
            painter.restore()
    finally:
        painter.end()
    return failures
