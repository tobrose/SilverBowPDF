"""The scrolling page view.

One PageWidget per page inside a vertical layout inside a QScrollArea. Widgets
are cheap when they aren't painted, and Qt only calls paintEvent on what's
visible, so laziness comes for free. This falls over somewhere north of a few
thousand pages, at which point the answer is a single custom-painted canvas
with virtualised page slots -- not worth the complexity yet.

PdfView is assembled from mixins, one per kind of work on the page, all
sharing its state: viewdraw (shapes, ink, stamps, signature boxes),
viewtext (text boxes, resizing), viewforms (filling and preparing forms).
This file keeps the view itself -- layout, zoom, navigation, selection,
mouse routing and the chrome painted over pages. Tools are in tools.py;
the small floating widgets in viewbars.py.
"""

from __future__ import annotations

import getpass
from dataclasses import replace
from typing import List, Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
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

from .annots import (
    HIGHLIGHT_COLORS,
    SHAPE_COLORS,
    STAMPS,
    TEXT_COLORS,
    Annot,
    describe,
    describe_annot,
    lay_markup,
    new_markup,
    without,
)
from .fields import TEXT
from .document import PdfDocument
from .rendering import PageRenderer
from .security import FULL_ACCESS
from .transform import PageTransform, normalize_rotation
from .tools import (
    FitMode,
    Tool,
    MARKUP_TOOLS,
    TEXT_TOOLS,
    DRAW_TOOLS,
    FIELD_TOOLS,
    FORM_TOOLS,
    SIGN_TOOLS,
    EDGE_CURSORS,
)
from .viewbars import MarkBar
from .viewdraw import DrawingMixin
from .viewforms import FormsMixin
from .viewtext import TextBoxMixin

PAGE_GAP = 14          # logical px between pages
VIEW_MARGIN = 18       # logical px around the page column
MIN_SCALE = 0.08
MAX_SCALE = 12.0
ZOOM_STEPS = [0.10, 0.25, 0.33, 0.50, 0.67, 0.75, 1.00, 1.25, 1.50,
              2.00, 3.00, 4.00, 6.00, 8.00]


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


class PdfView(FormsMixin, TextBoxMixin, DrawingMixin, QScrollArea):
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
    text_selection_changed = Signal(bool)      # whether any text is selected
    editing_changed = Signal(bool)             # a text box / field editor is open
    fields_edit = Signal(object, str, str)     # field key, new value, undo label
    forms_edit = Signal(object, object, str)   # page key, new widget tuple, undo label
    field_properties = Signal(int, object)     # page index, Widget (double-clicked)
    message = Signal(str)                      # something the user should know
    pages_rebuilt = Signal()                   # displayed indices may now mean other pages
    highlight_color_changed = Signal(object)   # picked on the mark bar
    text_style_changed = Signal()              # text_size / text_color, picked on the style bar
    text_layer_changed = Signal()              # pages gained or lost recognised (OCR) text
    signature_box = Signal(int, object)        # page index, PDF rect: sign with a certificate there

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: Optional[PdfDocument] = None
        self.renderer: Optional[PageRenderer] = None
        self._pages: List[PageWidget] = []
        self._scale = 1.0
        # View rotation. Always 0 since "rotate view" was removed (user's call:
        # a turned view on top of turned pages confused people); the plumbing
        # below it -- renderer, PageTransform -- still takes one and is tested
        # against pdfium with it, so it stays rather than being ripped out.
        self._rotation = 0
        self._fit = FitMode.WIDTH
        self._current_page = 0
        self._tool = Tool.SELECT
        self._selected = None   # (page key, annot ident) or None
        self._drag = None       # in-progress mouse drag, see page_mouse_press
        self._text_sel = None   # (page index, anchor char, focus char)
        self._search_hits = {}  # page index -> [(start, end, rects)], see set_search_hits
        self._search_current = None  # (page index, start) of the current match
        self._pan = None        # (last global pos, mouse button) while panning
        self._mark_bar = None   # MarkBar under a fresh text selection, see _show_mark_bar
        # What the document's author allows (security.py). MainWindow sets
        # it and gates every edit; this keeps the page from offering (a drag,
        # the mark bar, a field to type in) what would then be refused.
        self.access = FULL_ACCESS
        self.signature_image = None  # SignatureImage the picture-signature tool places
        # Names of the document's signed signature fields (MainWindow, from
        # checking them): Edit Fields leaves those alone, see _locked.
        self.signed_fields: frozenset = frozenset()
        self.highlight_color = HIGHLIGHT_COLORS["Yellow"]
        self.text_size = 12.0
        self.text_color = TEXT_COLORS["Black"]
        self.shape_color = SHAPE_COLORS["Red"]
        self.shape_width = 2.0
        self.stamp = STAMPS[0]  # (label, colour, dated, /Name)
        self._editor = None     # open text box editor, see _open_editor
        self._field_editor = None  # open form field editor, see _open_field
        self._selected_field = None  # (page key, widget ident) in Edit Fields
        self.author = getpass.getuser()  # /T on annotations created here
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

    # ---- tools, text selection and annotations ----------------------------

    @property
    def tool(self) -> Tool:
        return self._tool

    def set_tool(self, tool: Tool) -> None:
        if tool == self._tool:
            return
        self._tool = tool
        self._drag = None
        self._hide_mark_bar()  # it's a Select-tool thing
        if tool not in FORM_TOOLS:
            self._selected_field = None
        for page in self._pages:
            page.setCursor(self._tool_cursor())
            page.update()  # field outlines show only while preparing a form
        # The gaps between pages pan too, so they wear the hand as well.
        self._container.setCursor(Qt.OpenHandCursor if tool is Tool.PAN else Qt.ArrowCursor)
        self.tool_changed.emit(tool)

    def _tool_cursor(self):
        if self._tool is Tool.PAN:
            return Qt.OpenHandCursor
        if self._tool is Tool.NOTE or self._tool in DRAW_TOOLS or self._tool is Tool.STAMP \
                or self._tool in FIELD_TOOLS or self._tool in SIGN_TOOLS:
            return Qt.CrossCursor
        if self._tool in MARKUP_TOOLS or self._tool in TEXT_TOOLS:
            return Qt.IBeamCursor
        return Qt.ArrowCursor

    # -- text selection: (page index, anchor char, focus char), inclusive

    def text_selection(self):
        return self._text_sel

    def selected_text(self) -> str:
        if self._text_sel is None:
            return ""
        index, start, end = self._text_sel
        return self.document.text_in(index, start, end)

    def set_text_selection(self, index: int, start: int, end: int) -> None:
        old = self._text_sel
        self._text_sel = (index, start, end)
        if old != self._text_sel:
            self._hide_mark_bar()  # it belonged to the old selection
            self._update_pages({index} | ({old[0]} if old else set()))
            if old is None:
                self.text_selection_changed.emit(True)

    def clear_text_selection(self) -> None:
        self._hide_mark_bar()
        if self._text_sel is not None:
            index = self._text_sel[0]
            self._text_sel = None
            self._update_pages({index})
            self.text_selection_changed.emit(False)

    def select_all_text(self) -> bool:
        if self.document is None:
            return False
        index = self._current_page
        count = self.document.char_count(index)
        if count == 0:
            return False
        self.set_text_selection(index, 0, count - 1)
        return True

    def mark_selection(self, kind: str) -> bool:
        """Turn the text selection into a Highlight / Underline / StrikeOut."""
        if self._text_sel is None:
            return False
        index, start, end = self._text_sel
        rects = self.document.text_rects(index, start, end)
        if not rects:
            return False
        color = self.highlight_color if kind == "Highlight" else None
        mark = new_markup(kind, rects, self.author, color)
        self.clear_text_selection()
        self.annots_edit.emit(self.document.page_key(index),
                              lay_markup(self.document.page_annots(index), mark,
                                         self.document.char_boxes(index)),
                              f"Add {describe(kind)}")
        return True

    def _update_pages(self, indices) -> None:
        for i in indices:
            if 0 <= i < len(self._pages):
                self._pages[i].update()

    # -- the Highlight / Underline / Strikethrough bar under a selection

    def _show_mark_bar(self) -> None:
        """Offer to mark the text just selected with the mouse (Select
        tool). Sits under the selection's last line, or above its first
        when there's no room below; always inside the page."""
        self._hide_mark_bar()
        if self._tool is not Tool.SELECT or self._text_sel is None or not self.access.annotate:
            return
        index, start, end = self._text_sel
        page = self._pages[index]
        rects = [self._widget_rect(page, r).translated(QPointF(page.pos()))
                 for r in self.document.text_rects(index, start, end)]
        if not rects:
            return
        bar = MarkBar(self._container, self.highlight_color)
        bar.chosen.connect(self._mark_from_bar)
        gap = 6
        x = rects[-1].center().x() - bar.width() / 2
        y = max(r.bottom() for r in rects) + gap
        if y + bar.height() > page.geometry().bottom() - 4:
            y = min(r.top() for r in rects) - gap - bar.height()
        bar.move(*self._clamped(bar, x, y))
        bar.show()
        self._mark_bar = bar

    def _clamped(self, bar: QWidget, x: float, y: float):
        """Keep a bar inside the page column (it's a child of it)."""
        area = self._container
        return (int(max(4, min(x, area.width() - bar.width() - 4))),
                int(max(4, min(y, area.height() - bar.height() - 4))))

    def _hide_mark_bar(self) -> None:
        if self._mark_bar is not None:
            self._mark_bar.hide()
            self._mark_bar.deleteLater()
            self._mark_bar = None

    def _mark_from_bar(self, kind: str, color) -> None:
        if color is not None and color != self.highlight_color:
            self.highlight_color = color  # the swatch picked becomes the current colour
            self.highlight_color_changed.emit(color)
        self.mark_selection(kind)  # clears the selection, and with it the bar
        self.setFocus(Qt.OtherFocusReason)

    # -- search matches (found by findbar.FindBar; drawn here as chrome).
    # Repaint only the pages whose matches changed: on a remote desktop every
    # repaint is re-encoded and sent.

    def set_search_hits(self, index: int, hits) -> None:
        """Matches on one page: [(start, end, rects)], as find_text gives."""
        if hits:
            self._search_hits[index] = hits
        elif self._search_hits.pop(index, None) is None:
            return
        self._update_pages({index})

    def set_search_current(self, current) -> None:
        """(page index, start char) of the match to emphasise, or None."""
        old, self._search_current = self._search_current, current
        if old != current:
            self._update_pages({c[0] for c in (old, current) if c is not None})

    def clear_search(self) -> None:
        pages = set(self._search_hits) | ({self._search_current[0]} if self._search_current else set())
        self._search_hits = {}
        self._search_current = None
        self._update_pages(pages)

    def reveal(self, index: int, rects) -> None:
        """Scroll so these PDF-space rects on a page are in view, if they
        aren't already -- a third of the way down, where the eye goes."""
        if not 0 <= index < len(self._pages) or not rects:
            return
        page = self._pages[index]
        box = QRectF()
        for rect in rects:
            box = box.united(self._widget_rect(page, rect))
        box.translate(QPointF(page.pos()))
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        width, height = self.viewport().width(), self.viewport().height()
        if box.top() < vbar.value() or box.bottom() > vbar.value() + height:
            vbar.setValue(int(box.center().y() - height / 3))
        if box.left() < hbar.value() or box.right() > hbar.value() + width:
            hbar.setValue(int(box.center().x() - width / 2))

    # -- annotation selection

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

    def text_changed(self) -> None:
        """Recognised text was added or undone: the text layer's character
        indices are new, so a selection in the old ones means nothing."""
        self.clear_text_selection()
        self.text_layer_changed.emit()

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

    def _char_near(self, page: "PageWidget", pos: QPoint, exact: bool = False) -> int:
        x, y = page.pdf_point_at(pos)
        tolerance = 2.0 / max(page.widget_transform().scale, 1e-6)
        char = self.document.char_at(page.index, x, y, tolerance)
        if char < 0 and not exact:
            char = self.document.nearest_char(page.index, x, y)
        return char

    # -- mouse
    #
    # _drag is one of:
    #   {"mode": "move", ...}  dragging a (non-markup) annotation
    #   {"mode": "text", ...}  selecting text; "anchor" is set once the drag
    #                          passes the threshold, so a plain click selects
    #                          nothing (and, on a highlight, keeps it selected)

    # -- panning: the Pan tool's left drag, or a middle drag in any tool.
    # The gaps between pages pan as well (the mouse*Event overrides below).

    def _pan_press(self, event) -> bool:
        button = event.button()
        if self.document is None or self._pan is not None or not (
                button == Qt.MiddleButton or (button == Qt.LeftButton and self._tool is Tool.PAN)):
            return False
        self._pan = (event.globalPosition().toPoint(), button)
        QApplication.setOverrideCursor(Qt.ClosedHandCursor)
        return True

    def _pan_move(self, event) -> bool:
        if self._pan is None:
            return False
        pos = event.globalPosition().toPoint()
        delta = pos - self._pan[0]
        self._pan = (pos, self._pan[1])
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
        return True

    def _pan_release(self, event) -> bool:
        if self._pan is None or event.button() != self._pan[1]:
            return False
        self._pan = None
        QApplication.restoreOverrideCursor()
        return True

    def mousePressEvent(self, event) -> None:
        if not self._pan_press(event):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not self._pan_move(event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if not self._pan_release(event):
            super().mouseReleaseEvent(event)

    def page_mouse_press(self, page: "PageWidget", event) -> bool:
        self._hide_mark_bar()  # clicking anywhere else on a page dismisses it
        if self._pan_press(event):
            return True
        if event.button() != Qt.LeftButton or self.document is None:
            return False
        if self._editor is not None:
            # A click away from the text you're typing means "done": finish
            # it (which also leaves the text tool) and do nothing else --
            # not start another box, not select what's under the click.
            self.commit_text_edit()
            return True
        if self._field_editor is not None:
            # Finishing a field and clicking straight into the next one is
            # how forms get filled, so here the click carries on.
            self.commit_field_edit()
        self.setFocus(Qt.MouseFocusReason)
        pos = event.position().toPoint()
        if self._tool is Tool.NOTE:
            x, y = page.pdf_point_at(pos)
            self.note_requested.emit(page.index, x, y)
            return True
        if self._tool is Tool.STAMP:
            self._place_stamp(page, pos)
            return True
        if self._tool in FIELD_TOOLS:
            self._drag = {"mode": "fdraw", "page": page, "press": pos, "end": None}
            return True
        if self._tool in SIGN_TOOLS:
            self._drag = {"mode": "sigbox", "page": page, "press": pos, "end": None}
            return True
        if self._tool is Tool.FORM:
            return self._form_press(page, pos)
        if self._tool in DRAW_TOOLS:
            self._drag = {"mode": "draw", "page": page, "press": pos, "end": None,
                          "shift": False, "points": [page.pdf_point_at(pos)]}
            return True
        if self._tool in TEXT_TOOLS:
            hit = self._annot_at(page, pos)
            if hit is not None and hit.kind == "FreeText":
                self._edit_text(page, hit)  # the typewriter clicks into existing text
                return True
            self._drag = {"mode": "place", "page": page, "press": pos, "end": None}
            return True
        edge = self._resize_hit(page, pos) \
            if self._tool is Tool.SELECT and self.access.annotate else None
        if edge is not None:
            annot, edges = edge
            self.select_annot(page.index, annot.ident)
            self.clear_text_selection()
            left, top, right, bottom = page.widget_transform().pdf_rect_to_widget(annot.rect)
            self._drag = {"mode": "resize", "page": page, "annot": annot, "edges": edges,
                          "press": pos, "start": (left, top, right, bottom), "rect": None}
            return True
        annot = None if self._tool in MARKUP_TOOLS else self._annot_at(page, pos)
        if annot is None and self._tool is Tool.SELECT:
            widget = self._widget_at(page, pos, fillable_only=False)
            if widget is not None:
                self.select_annot(page.index, None)
                self.clear_text_selection()
                if self.access.fill_forms:
                    self._fill(page, widget)
                else:
                    self.message.emit("This document's author doesn't allow filling in "
                                      "its forms.")
                return True
        self.select_annot(page.index, annot.ident if annot else None)
        if annot is not None and not annot.is_markup and not self.access.annotate:
            return True  # selected, to read; not moved
        if annot is not None and not annot.is_markup:
            self.clear_text_selection()
            self._drag = {"mode": "move", "page": page, "annot": annot, "press": pos,
                          "start": page.pdf_point_at(pos), "delta": None}
        else:
            self._drag = {"mode": "text", "page": page, "press": pos, "anchor": None,
                          "annot": annot}
        return True

    def page_mouse_move(self, page: "PageWidget", event) -> bool:
        if self._pan_move(event):
            return True
        if self.document is None:
            return False
        pos = event.position().toPoint()
        drag = self._drag
        if drag is not None and event.buttons() & Qt.LeftButton and drag["page"] is page:
            started = drag.get("delta") is not None or drag.get("anchor") is not None \
                or drag.get("end") is not None or drag.get("rect") is not None
            if drag["mode"] in ("fdraw", "fmove", "fresize", "sigbox") \
                    and drag.get("end") is not None:
                started = True
            if not started and \
                    (pos - drag["press"]).manhattanLength() < QApplication.startDragDistance():
                return True
            if drag["mode"] == "resize":
                drag["rect"] = self._resized_rect(page, drag, pos)
                page.update()
                return True
            if drag["mode"] in ("fdraw", "fmove", "fresize", "sigbox"):
                drag["end"] = pos
                page.update()
                return True
            if drag["mode"] == "draw":
                drag["end"] = pos
                drag["shift"] = bool(event.modifiers() & Qt.ShiftModifier)
                if self._tool is Tool.PEN:
                    last = page.widget_transform().pdf_to_widget(*drag["points"][-1])
                    if abs(pos.x() - last[0]) + abs(pos.y() - last[1]) >= 2:
                        drag["points"].append(page.pdf_point_at(pos))
                page.update()
                return True
            if drag["mode"] == "place":
                return True
            if drag["mode"] == "move":
                x, y = page.pdf_point_at(pos)
                drag["delta"] = (x - drag["start"][0], y - drag["start"][1])
                page.update()
                return True
            if drag["anchor"] is None:
                anchor = self._char_near(page, drag["press"])
                if anchor < 0:
                    return True  # no text on this page
                drag["anchor"] = anchor
                if drag["annot"] is not None:
                    self.select_annot(page.index, None)  # a drag means text, not the mark
            focus = self._char_near(page, pos)
            if focus >= 0:
                self.set_text_selection(page.index, drag["anchor"], focus)
            return True
        if not event.buttons():
            self._hover(page, pos, event)
        return False

    def _hover(self, page: "PageWidget", pos: QPoint, event) -> None:
        if self._tool is Tool.FORM:
            widget = self._field_at(page, pos)
            if widget is not None and self._locked(widget):
                page.setCursor(Qt.ForbiddenCursor)
                return
            edges = self._edges_of(page, widget, pos) if widget is not None else ""
            page.setCursor(EDGE_CURSORS[edges] if edges else
                           Qt.SizeAllCursor if widget is not None else Qt.ArrowCursor)
            return
        if self._tool is not Tool.SELECT:
            return
        edge = self._resize_hit(page, pos) if self.access.annotate else None
        if edge is not None:
            page.setCursor(EDGE_CURSORS[edge[1]])
            QToolTip.hideText()
            return
        annot = self._annot_at(page, pos)
        widget = None if annot is not None else self._widget_at(page, pos)
        if annot is not None and not annot.is_markup and self.access.annotate:
            page.setCursor(Qt.SizeAllCursor)
        elif widget is not None:
            page.setCursor(Qt.IBeamCursor if widget.kind == TEXT else Qt.PointingHandCursor)
        elif self._char_near(page, pos, exact=True) >= 0:
            page.setCursor(Qt.IBeamCursor)
        else:
            page.setCursor(Qt.ArrowCursor)
        if annot is not None and annot.contents:
            who = f"{annot.author}: " if annot.author else ""
            QToolTip.showText(event.globalPosition().toPoint(), f"{who}{annot.contents}", page)
        else:
            QToolTip.hideText()

    def page_mouse_release(self, page: "PageWidget", event) -> bool:
        if self._pan_release(event):
            return True
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.LeftButton:
            return False
        if drag["mode"] == "fdraw":
            self._create_field(page, drag)
            page.update()
            return True
        if drag["mode"] == "sigbox":
            self._finish_sign_box(page, drag)
            page.update()
            return True
        if drag["mode"] in ("fmove", "fresize"):
            if drag["end"] is not None:
                self._commit_field_geometry(page, drag)
            page.update()
            return True
        if drag["mode"] == "draw":
            drag["shift"] = bool(event.modifiers() & Qt.ShiftModifier)
            if drag["end"] is not None:
                self._finish_drawing(page, drag)
            page.update()
            return True
        if drag["mode"] == "resize":
            if drag["rect"] is not None:
                old = drag["annot"]
                left, top, right, bottom = drag["rect"]
                rect = page.widget_transform().widget_rect_to_pdf((left, top, right, bottom))
                resized = replace(old, rect=rect, reshaped=True,
                                  fixed_width=old.fixed_width or old.kind == "FreeText")
                self.annots_edit.emit(self.document.page_key(page.index),
                                      tuple(resized if a.ident == old.ident else a
                                            for a in self.document.page_annots(page.index)),
                                      f"Resize {describe_annot(old)}")
            page.update()
            return True
        if drag["mode"] == "place":
            self._new_text(page, drag["press"], boxed=False, width=None)
            page.update()
            return True
        if drag["mode"] == "move":
            if drag["delta"] is not None:
                dx, dy = drag["delta"]
                moved = drag["annot"]
                annots = tuple(a.moved(dx, dy) if a.ident == moved.ident else a
                               for a in self.document.page_annots(page.index))
                self.annots_edit.emit(self.document.page_key(page.index), annots,
                                      f"Move {describe_annot(moved)}")
        elif drag["anchor"] is None:
            self.clear_text_selection()  # a click, not a drag
        elif self._tool in MARKUP_TOOLS:
            self.mark_selection(MARKUP_TOOLS[self._tool])
        else:
            self._show_mark_bar()
        page.update()
        return True

    def page_double_click(self, page: "PageWidget", event) -> bool:
        if self._tool is Tool.FORM and self.document is not None:
            found = self._field_at(page, event.position().toPoint())
            if found is not None and self._locked(found):
                return True  # the press already said why
            if found is not None:
                self.field_properties.emit(page.index, found)
                return True
            return False
        if self._tool is not Tool.SELECT or self.document is None:
            return False
        pos = event.position().toPoint()
        annot = self._annot_at(page, pos) if self.access.annotate else None
        if annot is not None and annot.kind == "FreeText":
            self._edit_text(page, annot)
            return True
        if annot is not None:
            self.annot_open.emit(page.index, annot)
            return True
        char = self._char_near(page, pos, exact=True)
        if char >= 0:
            self.set_text_selection(page.index, *self.document.word_at(page.index, char))
            self._show_mark_bar()
            return True
        return False

    # -- in-place editors: text boxes (viewtext.py), form fields (viewforms.py)

    def is_editing(self) -> bool:
        return self._editor is not None or self._field_editor is not None

    def delete_selected_annot(self) -> bool:
        found = self.selected_annot()
        if found is None:
            return False
        index, gone = found
        annots = without(self.document.page_annots(index), gone)
        self._selected = None
        self.annots_edit.emit(self.document.page_key(index), annots, f"Delete {describe_annot(gone)}")
        return True

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.access.annotate and (
                self.delete_selected_field() or self.delete_selected_annot()):
            return
        if event.key() == Qt.Key_Escape:
            self._drag = None
            self.set_tool(Tool.SELECT)
            self.clear_text_selection()
            if self._selected is not None:
                self.select_annot(0, None)
            return
        super().keyPressEvent(event)

    def paint_page_chrome(self, page: "PageWidget", painter: QPainter) -> None:
        """Text selection, and the selected annotation's outline and drag
        preview. UI chrome only -- annotations are always drawn by pdfium."""
        drag = self._drag
        if self._tool in FORM_TOOLS:
            self._paint_fields(page, painter)
        if drag is not None and drag["mode"] == "draw" and drag["page"] is page \
                and drag["end"] is not None:
            self._paint_drawing(page, painter, drag)
        if drag is not None and drag["mode"] == "sigbox" and drag["page"] is page \
                and drag["end"] is not None:
            a, b = drag["press"], drag["end"]
            box = QRectF(QPointF(min(a.x(), b.x()), min(a.y(), b.y())),
                         QPointF(max(a.x(), b.x()), max(a.y(), b.y())))
            painter.fillRect(box, QColor(40, 110, 230, 30))
            painter.setPen(QPen(QColor(40, 110, 230), 1.5, Qt.DashLine))
            painter.drawRect(box)
        if drag is not None and drag["mode"] == "resize" and drag["page"] is page \
                and drag["rect"] is not None:
            left, top, right, bottom = drag["rect"]
            box = QRectF(left, top, right - left, bottom - top)
            painter.fillRect(box, QColor(40, 110, 230, 30))
            painter.setPen(QPen(QColor(40, 110, 230), 1.5, Qt.DashLine))
            painter.drawRect(box)
        if self._text_sel is not None and self._text_sel[0] == page.index:
            _, start, end = self._text_sel
            for rect in self.document.text_rects(page.index, start, end):
                painter.fillRect(self._widget_rect(page, rect), QColor(0, 120, 215, 70))
        for start, _, rects in self._search_hits.get(page.index, ()):
            current = self._search_current == (page.index, start)
            for rect in rects:
                box = self._widget_rect(page, rect)
                painter.fillRect(box, QColor(255, 140, 0, 140) if current
                                 else QColor(255, 210, 0, 90))
                if current:
                    painter.setPen(QPen(QColor(220, 100, 0), 1.5))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(box.adjusted(-1, -1, 1, 1))

        found = self.selected_annot()
        if found is None or found[0] != page.index:
            return
        annot = found[1]
        rect = annot.rect
        drag = self._drag
        if drag is not None and drag["mode"] == "move" and drag["page"] is page \
                and drag["delta"] is not None:
            rect = annot.moved(*drag["delta"]).rect
            painter.fillRect(self._widget_rect(page, rect), QColor(40, 110, 230, 40))
        r = self._widget_rect(page, rect).adjusted(-3, -3, 3, 3)
        painter.setPen(QPen(QColor(40, 110, 230), 1.5, Qt.DashLine))
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
        self.commit_text_edit()
        self.commit_field_edit()
        if self.document is not None:
            self.document.close()
        self.document = document
        self._selected = None
        self._drag = None
        if self._pan is not None:  # e.g. a middle-drag when a file was dropped
            self._pan = None
            QApplication.restoreOverrideCursor()
        if self._text_sel is not None:
            self._text_sel = None
            self.text_selection_changed.emit(False)
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
        self.commit_text_edit()
        self.commit_field_edit()
        self.commit_field_edit()
        if self.renderer is not None:
            self.renderer.clear()  # cache is keyed by displayed index, now stale
        self.clear_text_selection()  # indexes a page that may have moved
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
        self._hide_mark_bar()  # before its page is deleted under it
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._pages = []
        # Matches are by displayed index; after a rebuild that may be another page.
        self._search_hits = {}
        self._search_current = None

        if self.document is None:
            self._placeholder = QLabel("Open a PDF to get started  \u2014  Ctrl+O, or drop a file here")
            self._placeholder.setAlignment(Qt.AlignCenter)
            self._placeholder.setStyleSheet("color: #cfcfd4; font-size: 15px; padding: 60px;")
            self._layout.addWidget(self._placeholder)
            self.pages_rebuilt.emit()
            return

        for i in range(self.document.page_count):
            page = PageWidget(self.document, self.renderer, i, self._container, view=self)
            page.apply_view_state(self._scale, self._rotation)
            page.setCursor(self._tool_cursor())
            self._layout.addWidget(page, 0, Qt.AlignHCenter)
            # Shown now, not by Qt's deferred show for children added to a
            # visible parent: until then the layout skips them as hidden and
            # sizes the column to the viewport, so the first paint found
            # EVERY page stacked on screen and rendered the whole document.
            page.show()
            self._pages.append(page)
        self._layout.addStretch(1)
        self.pages_rebuilt.emit()

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
        self.commit_text_edit()  # the editor is sized for the old zoom
        self.commit_field_edit()
        self._hide_mark_bar()    # so is the mark bar's position

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
