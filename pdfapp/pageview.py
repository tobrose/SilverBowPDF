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

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .annots import Annot, describe, without
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


class Tool(Enum):
    SELECT = "select"
    NOTE = "note"


class PageWidget(QWidget):
    """A single rendered page."""

    def __init__(self, document: PdfDocument, renderer: PageRenderer, index: int,
                 parent=None, view: Optional["PdfView"] = None):
        super().__init__(parent)
        self.document = document
        self.renderer = renderer
        self.index = index
        self.view = view
        self._scale = 1.0
        self._rotation = 0
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)  # hover cursor and note tooltips
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
        """Widget coordinates -> PDF user space. Mouse input only; see
        widget_transform for why this must not use the render scale."""
        return self.widget_transform().widget_to_pdf(pos.x(), pos.y())

    # ---- input: all of it is the view's business -------------------------

    def mousePressEvent(self, event) -> None:
        if self.view is None or not self.view.page_mouse_press(self, event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.view is None or not self.view.page_mouse_move(self, event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.view is None or not self.view.page_mouse_release(self, event):
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self.view is None or not self.view.page_double_click(self, event):
            super().mouseDoubleClickEvent(event)

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
        if self.view is not None:
            self.view.paint_page_chrome(self, painter)

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
    # Annotation requests. The view never edits: MainWindow owns the undo
    # history and turns these into DocState edits.
    annots_edit = Signal(object, object, str)  # page key, new annot tuple, undo label
    annot_open = Signal(int, object)           # page index, Annot (double-clicked)
    note_requested = Signal(int, float, float) # page index, PDF x, y
    tool_changed = Signal(object)              # Tool

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: Optional[PdfDocument] = None
        self.renderer: Optional[PageRenderer] = None
        self._pages: List[PageWidget] = []
        self._scale = 1.0
        self._rotation = 0
        self._fit = FitMode.WIDTH
        self._current_page = 0
        self._tool = Tool.SELECT
        self._selected = None   # (page key, annot ident) or None
        self._drag = None       # in-progress annotation move, see page_mouse_press
        self.setFocusPolicy(Qt.StrongFocus)  # Delete and Esc for annotations

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

    # ---- tools and annotations -------------------------------------------

    @property
    def tool(self) -> Tool:
        return self._tool

    def set_tool(self, tool: Tool) -> None:
        if tool == self._tool:
            return
        self._tool = tool
        self._drag = None
        cursor = Qt.CrossCursor if tool is Tool.NOTE else Qt.ArrowCursor
        for page in self._pages:
            page.setCursor(cursor)
        self.tool_changed.emit(tool)

    def select_annot(self, index: int, ident) -> None:
        self._selected = (self.document.page_key(index), ident) if ident is not None else None
        for page in self._pages:
            page.update()

    def selected_annot(self):
        """(page index, Annot) of the selected annotation, or None."""
        if self._selected is None or self.document is None:
            return None
        key, ident = self._selected
        for page in self._pages:
            if self.document.page_key(page.index) == key:
                for annot in self.document.page_annots(page.index):
                    if annot.ident == ident:
                        return page.index, annot
        return None

    def annots_changed(self) -> None:
        """Annotation edits changed (edit, undo, redo). Sizes don't change,
        so repaint only; the render cache keys on the edits themselves."""
        if self.selected_annot() is None:
            self._selected = None
        for page in self._pages:
            page.update()

    def _annot_at(self, page: "PageWidget", pos: QPoint) -> Optional[Annot]:
        x, y = page.pdf_point_at(pos)
        slop = 3.0 / max(page.widget_transform().scale, 1e-6)  # ~3 screen px
        for annot in reversed(self.document.page_annots(page.index)):  # topmost first
            if annot.contains(x, y, slop):
                return annot
        return None

    def page_mouse_press(self, page: "PageWidget", event) -> bool:
        if event.button() != Qt.LeftButton or self.document is None:
            return False
        self.setFocus(Qt.MouseFocusReason)
        pos = event.position().toPoint()
        if self._tool is Tool.NOTE:
            x, y = page.pdf_point_at(pos)
            self.note_requested.emit(page.index, x, y)
            return True
        annot = self._annot_at(page, pos)
        self.select_annot(page.index, annot.ident if annot else None)
        if annot is not None:
            self._drag = {"page": page, "annot": annot, "press": pos,
                          "start": page.pdf_point_at(pos), "delta": None}
        return True

    def page_mouse_move(self, page: "PageWidget", event) -> bool:
        if self.document is None:
            return False
        pos = event.position().toPoint()
        drag = self._drag
        if drag is not None and event.buttons() & Qt.LeftButton and drag["page"] is page:
            if drag["delta"] is None and \
                    (pos - drag["press"]).manhattanLength() < QApplication.startDragDistance():
                return True
            x, y = page.pdf_point_at(pos)
            drag["delta"] = (x - drag["start"][0], y - drag["start"][1])
            page.update()
            return True
        if self._tool is Tool.SELECT and not event.buttons():
            annot = self._annot_at(page, pos)
            page.setCursor(Qt.SizeAllCursor if annot else Qt.ArrowCursor)
            if annot is not None and annot.contents:
                who = f"{annot.author}: " if annot.author else ""
                QToolTip.showText(event.globalPosition().toPoint(),
                                  f"{who}{annot.contents}", page)
            else:
                QToolTip.hideText()
        return False

    def page_mouse_release(self, page: "PageWidget", event) -> bool:
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.LeftButton:
            return False
        if drag["delta"] is not None:
            dx, dy = drag["delta"]
            moved = drag["annot"]
            annots = tuple(a.moved(dx, dy) if a.ident == moved.ident else a
                           for a in self.document.page_annots(page.index))
            self.annots_edit.emit(self.document.page_key(page.index), annots,
                                  f"Move {describe(moved.kind)}")
        page.update()
        return True

    def page_double_click(self, page: "PageWidget", event) -> bool:
        if self._tool is not Tool.SELECT or self.document is None:
            return False
        annot = self._annot_at(page, event.position().toPoint())
        if annot is None:
            return False
        self.annot_open.emit(page.index, annot)
        return True

    def delete_selected_annot(self) -> bool:
        found = self.selected_annot()
        if found is None:
            return False
        index, gone = found
        annots = without(self.document.page_annots(index), gone)
        self._selected = None
        self.annots_edit.emit(self.document.page_key(index), annots, f"Delete {describe(gone.kind)}")
        return True

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.delete_selected_annot():
            return
        if event.key() == Qt.Key_Escape:
            self._drag = None
            self.set_tool(Tool.SELECT)
            if self._selected is not None:
                self.select_annot(0, None)
            return
        super().keyPressEvent(event)

    def paint_page_chrome(self, page: "PageWidget", painter: QPainter) -> None:
        """Selection outline (and drag preview) over a rendered page. UI
        chrome only -- the annotation itself is always drawn by pdfium."""
        found = self.selected_annot()
        if found is None or found[0] != page.index:
            return
        annot = found[1]
        rect = annot.rect
        drag = self._drag
        if drag is not None and drag["page"] is page and drag["delta"] is not None:
            rect = annot.moved(*drag["delta"]).rect
            painter.fillRect(self._widget_rect(page, rect), QColor(40, 110, 230, 40))
        r = self._widget_rect(page, rect).adjusted(-3, -3, 3, 3)
        pen = QPen(QColor(40, 110, 230), 1.5, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(r)

    @staticmethod
    def _widget_rect(page: "PageWidget", pdf_rect) -> QRectF:
        left, top, right, bottom = page.widget_transform().pdf_rect_to_widget(pdf_rect)
        return QRectF(left, top, right - left, bottom - top)

    # ---- document ------------------------------------------------------

    def set_document(self, document: Optional[PdfDocument], keep_view: bool = False) -> None:
        """Show `document`, closing the previous one.

        keep_view keeps zoom, view rotation and page position -- for reopening
        the same file after a save, which shouldn't feel like opening it.
        """
        if self.document is not None:
            self.document.close()
        self.document = document
        self._selected = None
        self._drag = None
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
            page = PageWidget(self.document, self.renderer, i, self._container, view=self)
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
