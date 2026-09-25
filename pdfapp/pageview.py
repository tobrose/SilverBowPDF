"""The scrolling page view.

One PageWidget per page inside a vertical layout inside a QScrollArea. Widgets
are cheap when they aren't painted, and Qt only calls paintEvent on what's
visible, so laziness comes for free. This falls over somewhere north of a few
thousand pages, at which point the answer is a single custom-painted canvas
with virtualised page slots -- not worth the complexity yet.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .document import PdfDocument
from .rendering import PageRenderer
from .transform import PageTransform, normalize_rotation

PAGE_GAP = 14          # logical px between pages
VIEW_MARGIN = 18       # logical px around the page column
MIN_SCALE = 0.08
MAX_SCALE = 12.0
ZOOM_STEPS = [0.10, 0.25, 0.33, 0.50, 0.67, 0.75, 1.00, 1.25, 1.50,
              2.00, 3.00, 4.00, 6.00, 8.00]


class FitMode(Enum):
    FIXED = "fixed"
    WIDTH = "width"
    PAGE = "page"


class PageWidget(QWidget):
    """A single rendered page."""

    def __init__(self, document: PdfDocument, renderer: PageRenderer, index: int, parent=None):
        super().__init__(parent)
        self.document = document
        self.renderer = renderer
        self.index = index
        self._scale = 1.0
        self._rotation = 0
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.apply_view_state(1.0, 0)

    # ---- geometry ------------------------------------------------------

    def widget_transform(self) -> PageTransform:
        """Transform in *logical* pixels -- the one to use for mouse hit-testing.

        Deliberately not the same as the render transform: on a HiDPI display
        the bitmap is devicePixelRatio times larger than the widget. Using the
        render transform for mouse input is how you get clicks that land at
        half or double the intended position on a Retina screen and nowhere
        else, which is a delightful bug to debug remotely.
        """
        return self.document.transform(self.index, self._scale, self._rotation, offset=(0.0, 0.0))

    def render_scale(self) -> float:
        return self._scale * self.devicePixelRatioF()

    def apply_view_state(self, scale: float, rotation: int) -> None:
        self._scale = scale
        self._rotation = normalize_rotation(rotation)
        w, h = self.widget_transform().bitmap_size
        self.setFixedSize(w, h)
        self.update()

    def pdf_point_at(self, pos: QPoint):
        """Widget coordinates -> PDF user space. For annotations later."""
        return self.widget_transform().widget_to_pdf(pos.x(), pos.y())

    # ---- painting ------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.rect()

        image = self.renderer.cached(self.index, self.render_scale(), self._rotation)
        if image is None:
            try:
                image = self.renderer.image(self.index, self.render_scale(), self._rotation)
            except Exception as exc:  # a broken page shouldn't kill the window
                painter.fillRect(rect, QColor("#ffffff"))
                painter.setPen(QColor("#b00020"))
                painter.drawText(
                    rect.adjusted(12, 12, -12, -12),
                    Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap,
                    f"Page {self.index + 1} failed to render:\n{exc}",
                )
                self._draw_border(painter, rect)
                return

        image.setDevicePixelRatio(self.devicePixelRatioF())
        painter.drawImage(QPoint(0, 0), image)
        self._draw_border(painter, rect)

    @staticmethod
    def _draw_border(painter: QPainter, rect: QRect) -> None:
        pen = QPen(QColor(0, 0, 0, 60))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))


class PdfView(QScrollArea):
    """Continuous vertical scroll view over a document."""

    page_changed = Signal(int)        # 0-based
    zoom_changed = Signal(float)
    document_opened = Signal(object)  # PdfDocument or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: Optional[PdfDocument] = None
        self.renderer: Optional[PageRenderer] = None
        self._pages: List[PageWidget] = []
        self._scale = 1.0
        self._rotation = 0
        self._fit = FitMode.WIDTH
        self._current_page = 0

        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundRole(self.backgroundRole())
        self.viewport().setStyleSheet("background: #3a3a3e;")

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(VIEW_MARGIN, VIEW_MARGIN, VIEW_MARGIN, VIEW_MARGIN)
        self._layout.setSpacing(PAGE_GAP)
        self._layout.setAlignment(Qt.AlignHCenter)
        self.setWidget(self._container)

        self._placeholder = QLabel("Open a PDF to get started  \u2014  Ctrl+O, or drop a file here")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #cfcfd4; font-size: 15px; padding: 60px;")
        self._layout.addWidget(self._placeholder)

        self.verticalScrollBar().valueChanged.connect(self._update_current_page)

    # ---- document ------------------------------------------------------

    def set_document(self, document: Optional[PdfDocument], keep_view: bool = False) -> None:
        """Show `document`, closing the previous one.

        keep_view keeps zoom, view rotation and page position -- for reopening
        the same file after a save, which shouldn't feel like opening it.
        """
        if self.document is not None:
            self.document.close()
        self.document = document
        self.renderer = PageRenderer(document) if document is not None else None
        target = self._current_page if keep_view else 0
        if not keep_view:
            self._rotation = 0
        self._current_page = 0
        self._rebuild()
        self.document_opened.emit(document)
        if document is not None:
            self._apply_fit()
            self._layout.activate()
            self.scroll_to_page(target)

    def pages_changed(self, current: int) -> None:
        """The document's page list was edited. Rebuild, keeping zoom and view."""
        if self.document is None:
            return
        if self.renderer is not None:
            self.renderer.clear()  # cache is keyed by displayed index, now stale
        self._rebuild()
        self._apply_fit()  # the widest page may have been deleted or rotated
        # New widgets have no position until the layout runs; scrolling to one
        # before that lands on y=0.
        self._layout.activate()
        self.scroll_to_page(current)

    def page_at(self, viewport_pos: QPoint) -> Optional[int]:
        """Index of the page under a viewport position, or None for the gap."""
        pos = self._container.mapFrom(self.viewport(), viewport_pos)
        for page in self._pages:
            if page.geometry().contains(pos):
                return page.index
        return None

    def _rebuild(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._pages = []

        if self.document is None:
            self._placeholder = QLabel("Open a PDF to get started  \u2014  Ctrl+O, or drop a file here")
            self._placeholder.setAlignment(Qt.AlignCenter)
            self._placeholder.setStyleSheet("color: #cfcfd4; font-size: 15px; padding: 60px;")
            self._layout.addWidget(self._placeholder)
            return

        for i in range(self.document.page_count):
            page = PageWidget(self.document, self.renderer, i, self._container)
            page.apply_view_state(self._scale, self._rotation)
            self._layout.addWidget(page, 0, Qt.AlignHCenter)
            self._pages.append(page)
        self._layout.addStretch(1)

    # ---- zoom ----------------------------------------------------------

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def rotation(self) -> int:
        return self._rotation

    @property
    def fit_mode(self) -> FitMode:
        return self._fit

    def set_scale(self, scale: float, fit: FitMode = FitMode.FIXED, anchor: Optional[QPoint] = None) -> None:
        scale = max(MIN_SCALE, min(MAX_SCALE, scale))
        if abs(scale - self._scale) < 1e-6 and fit == self._fit:
            return

        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        if anchor is None:
            anchor = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        # Content-space point under the anchor, as a ratio we can restore after
        # the relayout. Margins don't scale, so this is accurate to a few px --
        # close enough that the page doesn't visibly jump.
        before_x = hbar.value() + anchor.x()
        before_y = vbar.value() + anchor.y()
        factor = scale / self._scale if self._scale else 1.0

        self._scale = scale
        self._fit = fit
        if self.renderer is not None:
            self.renderer.clear()
        for page in self._pages:
            page.apply_view_state(self._scale, self._rotation)

        self._container.adjustSize()
        hbar.setValue(int(round(before_x * factor - anchor.x())))
        vbar.setValue(int(round(before_y * factor - anchor.y())))
        self.zoom_changed.emit(self._scale)
        self._update_current_page()

    def zoom_in(self) -> None:
        for step in ZOOM_STEPS:
            if step > self._scale + 1e-6:
                self.set_scale(step)
                return
        self.set_scale(self._scale * 1.25)

    def zoom_out(self) -> None:
        for step in reversed(ZOOM_STEPS):
            if step < self._scale - 1e-6:
                self.set_scale(step)
                return
        self.set_scale(self._scale / 1.25)

    def _available_width(self) -> int:
        return max(self.viewport().width() - 2 * VIEW_MARGIN - 4, 50)

    def _available_height(self) -> int:
        return max(self.viewport().height() - 2 * VIEW_MARGIN - 4, 50)

    def fit_width(self) -> None:
        if self.document is None:
            return
        widest = self.document.widest_display_width_pt(self._rotation)
        self.set_scale(self._available_width() / widest, FitMode.WIDTH)

    def fit_page(self) -> None:
        if self.document is None:
            return
        widest = self.document.widest_display_width_pt(self._rotation)
        tallest = self.document.tallest_display_height_pt(self._rotation)
        self.set_scale(
            min(self._available_width() / widest, self._available_height() / tallest),
            FitMode.PAGE,
        )

    def _apply_fit(self) -> None:
        if self._fit is FitMode.WIDTH:
            self.fit_width()
        elif self._fit is FitMode.PAGE:
            self.fit_page()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit is not FitMode.FIXED:
            self._apply_fit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                anchor = event.position().toPoint()
                factor = 1.15 if delta > 0 else 1 / 1.15
                self.set_scale(self._scale * factor, FitMode.FIXED, anchor)
            event.accept()
            return
        super().wheelEvent(event)

    # ---- rotation ------------------------------------------------------

    def rotate_view(self, delta: int) -> None:
        """Rotate the *view*. Does not modify the document."""
        if self.document is None:
            return
        self._rotation = normalize_rotation(self._rotation + delta)
        if self.renderer is not None:
            self.renderer.clear()
        for page in self._pages:
            page.apply_view_state(self._scale, self._rotation)
        self._apply_fit()
        self._container.adjustSize()
        self.scroll_to_page(self._current_page)

    # ---- navigation ----------------------------------------------------

    @property
    def current_page(self) -> int:
        return self._current_page

    def scroll_to_page(self, index: int) -> None:
        if not self._pages:
            return
        index = max(0, min(len(self._pages) - 1, index))
        target = self._pages[index]
        y = target.pos().y() - VIEW_MARGIN // 2
        self.verticalScrollBar().setValue(max(0, y))
        self._current_page = index
        self.page_changed.emit(index)

    def next_page(self) -> None:
        self.scroll_to_page(self._current_page + 1)

    def previous_page(self) -> None:
        self.scroll_to_page(self._current_page - 1)

    def _update_current_page(self) -> None:
        """The current page is whichever one owns the viewport's midline."""
        if not self._pages:
            return
        midline = self.verticalScrollBar().value() + self.viewport().height() // 3
        found = 0
        for i, page in enumerate(self._pages):
            top = page.pos().y()
            if top <= midline:
                found = i
            else:
                break
        if found != self._current_page:
            self._current_page = found
            self.page_changed.emit(found)
