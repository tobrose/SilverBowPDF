"""The scrolling page view.

One PageWidget per page inside a vertical layout inside a QScrollArea. Widgets
are cheap when they aren't painted, and Qt only calls paintEvent on what's
visible, so laziness comes for free. This falls over somewhere north of a few
thousand pages, at which point the answer is a single custom-painted canvas
with virtualised page slots -- not worth the complexity yet.
"""

from __future__ import annotations

import getpass
import math
import time
from dataclasses import replace
from enum import Enum
from typing import List, Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap,
                           QTextCursor, QWheelEvent)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .annots import (
    HIGHLIGHT_COLORS,
    SHAPE_COLORS,
    STAMPS,
    TEXT_COLORS,
    TEXT_SIZES,
    Annot,
    describe,
    describe_annot,
    lay_markup,
    new_ink,
    new_line,
    new_markup,
    new_shape,
    new_stamp,
    new_signature_image,
    new_text,
    signature_click_box,
    text_box_size,
    without,
)
from .fields import (
    CHECKBOX,
    COMBO,
    NEW_FIELD_SIZES,
    RADIO,
    TEXT,
    Widget,
    auto_font_size,
    current_value,
    is_on,
    new_field,
    unique_name,
)
from .textlayout import PADDING, encodable, layout
from .document import PdfDocument
from .rendering import PageRenderer
from .security import FULL_ACCESS
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
    PAN = "pan"                 # drag to scroll; sticky until Esc
    NOTE = "note"
    HIGHLIGHT = "highlight"
    UNDERLINE = "underline"
    STRIKEOUT = "strikeout"
    TYPEWRITER = "typewriter"
    RECT = "rect"
    ELLIPSE = "ellipse"
    LINE = "line"
    ARROW = "arrow"
    PEN = "pen"
    STAMP = "stamp"
    FORM = "form"               # edit fields: select, move, resize, delete, properties
    FIELD_TEXT = "field_text"
    FIELD_MULTILINE = "field_multiline"
    FIELD_CHECKBOX = "field_checkbox"
    FIELD_COMBO = "field_combo"
    SIGN = "sign"               # certificate signature: drag its box, then MainWindow signs
    SIGNATURE = "signature"     # picture signature: an image stamp, no certificate


# Tools that mark text: drag across it, a markup annotation lands on release.
MARKUP_TOOLS = {Tool.HIGHLIGHT: "Highlight", Tool.UNDERLINE: "Underline",
                Tool.STRIKEOUT: "StrikeOut"}
# Typewriter only: the separate Text Box tool was removed (user's call).
# Bordered boxes from files, or made before, still open and resize.
TEXT_TOOLS = (Tool.TYPEWRITER,)
# Drag to draw. Shapes return to Select once drawn; the pen stays until Esc.
DRAW_TOOLS = (Tool.RECT, Tool.ELLIPSE, Tool.LINE, Tool.ARROW, Tool.PEN)
RESIZABLE_SHAPES = ("Square", "Circle")
# Draw a field (drag, or click for a standard size); then Edit Fields.
FIELD_TOOLS = {Tool.FIELD_TEXT: (TEXT, False), Tool.FIELD_MULTILINE: (TEXT, True),
               Tool.FIELD_CHECKBOX: (CHECKBOX, False), Tool.FIELD_COMBO: (COMBO, False)}
FORM_TOOLS = (Tool.FORM, *FIELD_TOOLS)
# Drag a box (or click for a standard size); both return to Select after.
SIGN_TOOLS = (Tool.SIGN, Tool.SIGNATURE)
SIGN_BOX = (216.0, 72.0)  # points, as read: a certificate signature placed with a click

_EDGE_CURSORS = {
    "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
    "lt": Qt.SizeFDiagCursor, "rb": Qt.SizeFDiagCursor, "rt": Qt.SizeBDiagCursor,
    "lb": Qt.SizeBDiagCursor,
}


class _FieldEditor(QPlainTextEdit):
    """In-place editor for a form text field. Enter finishes a one-line
    field; Tab / Shift+Tab finish and move to the next / previous field."""

    finished = Signal(int)  # 0 done, +1 next field, -1 previous field

    def __init__(self, parent, multiline: bool):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if multiline else QPlainTextEdit.NoWrap)
        self._multiline = multiline
        self._done = False

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key_Backtab or (key == Qt.Key_Tab and event.modifiers() & Qt.ShiftModifier):
            self.finish(-1)
        elif key == Qt.Key_Tab:
            self.finish(+1)
        elif key == Qt.Key_Escape:
            self.finish(0)
        elif key in (Qt.Key_Return, Qt.Key_Enter) and (
                not self._multiline or event.modifiers() & Qt.ControlModifier):
            self.finish(0)
        else:
            super().keyPressEvent(event)

    def focusNextPrevChild(self, forward: bool) -> bool:
        return False  # Tab is ours, not Qt's focus chain

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.finish(0)

    def finish(self, move: int) -> None:
        if not self._done:
            self._done = True
            self.finished.emit(move)

    def reopen(self) -> None:
        self._done = False
        self.setFocus()


class _TextEditor(QPlainTextEdit):
    """The in-place editor for a text box. Finishes on Esc, Ctrl+Enter or
    losing focus -- clicking anywhere else is how you're done typing."""

    finished = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self._done = False

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape or (
                event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and event.modifiers() & Qt.ControlModifier):
            self.finish()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.finish()

    def finish(self) -> None:
        if not self._done:
            self._done = True
            self.finished.emit()

    def reopen(self) -> None:
        self._done = False
        self.setFocus()


def _swatch_icon(color, size: int = 12) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor.fromRgbF(*color))
    return QIcon(pixmap)


class _Bar(QFrame):
    """A small row of buttons floating by the text: the mark bar under a
    selection, the style bar over the typewriter. A child of the scrolling
    page column, not a popup window -- it scrolls with the page, can't grab
    the next click, and costs a remote desktop one small repaint -- and not of the page
    itself, which would clip it when zoomed out narrower than the bar. Its
    buttons never take focus, so keys stay with the view, and a text editor
    being typed in keeps its caret."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("bar")
        # Explicit colours throughout: the viewport's dark stylesheet
        # cascades to every child otherwise.
        self.setStyleSheet(
            "#bar { background: #ffffff; border: 1px solid #9a9aa2; border-radius: 4px; }"
            "QToolButton { background: transparent; color: #1e1e1e; border: 1px solid transparent;"
            " border-radius: 3px; padding: 3px 6px; }"
            "QToolButton:hover { background: #e3ecfb; }"
            "QToolButton:pressed { background: #c9dbf7; }"
            "QToolButton:checked { border-color: #2868e6; background: #e3ecfb; }"
            "QLabel { color: #5a5a60; background: transparent; padding: 0 2px 0 4px; }")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(3, 2, 3, 2)
        self._row.setSpacing(1)

    def button(self, text: str = "", icon: Optional[QIcon] = None, tip: str = "",
               checked: Optional[bool] = None) -> QToolButton:
        b = QToolButton(self, text=text, autoRaise=True, focusPolicy=Qt.NoFocus, toolTip=tip)
        if icon is not None:
            b.setIcon(icon)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if text else Qt.ToolButtonIconOnly)
        if checked is not None:
            b.setCheckable(True)
            b.setChecked(checked)
        b.setCursor(Qt.PointingHandCursor)
        self._row.addWidget(b)
        return b

    def label(self, text: str) -> None:
        self._row.addWidget(QLabel(text, self))

    def gap(self) -> None:
        self._row.addSpacing(8)


class _MarkBar(_Bar):
    """Under a fresh text selection (Select tool): Highlight in the current
    colour or one of the others, Underline, Strikethrough."""

    chosen = Signal(str, object)  # kind, highlight colour (None: current / not a highlight)

    def __init__(self, parent, highlight_color):
        super().__init__(parent)
        self.button("Highlight", _swatch_icon(highlight_color),
                    "Highlight in the current colour").clicked.connect(
            lambda: self.chosen.emit("Highlight", None))
        for name, color in HIGHLIGHT_COLORS.items():
            self.button(icon=_swatch_icon(color), tip=f"Highlight {name.lower()}").clicked.connect(
                lambda _=False, c=color: self.chosen.emit("Highlight", c))
        self.gap()
        self.button("Underline").clicked.connect(lambda: self.chosen.emit("Underline", None))
        self.button("Strikethrough").clicked.connect(lambda: self.chosen.emit("StrikeOut", None))
        self.adjustSize()


class _StyleBar(_Bar):
    """Over the typewriter while typing: text colour and size. Changing
    them restyles what's being typed, and becomes the default for the next."""

    chosen = Signal(object, object)  # size or None, colour or None

    def __init__(self, parent, size: float, color):
        super().__init__(parent)
        colors, sizes = QButtonGroup(self), QButtonGroup(self)  # one ticked in each
        self.label("Colour")
        for name, c in TEXT_COLORS.items():
            b = self.button(icon=_swatch_icon(c, 14), tip=name, checked=tuple(c) == tuple(color))
            colors.addButton(b)
            b.clicked.connect(lambda _=False, c=c: self.chosen.emit(None, c))
        self.gap()
        self.label("Size")
        for s in TEXT_SIZES:
            b = self.button(str(s), tip=f"{s} pt", checked=abs(s - size) < 0.01)
            sizes.addButton(b)
            b.clicked.connect(lambda _=False, s=s: self.chosen.emit(float(s), None))
        self.adjustSize()


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
        self._mark_bar = None   # _MarkBar under a fresh text selection, see _show_mark_bar
        # What the document's author allows (security.py). MainWindow sets
        # it and gates every edit; this keeps the page from offering (a drag,
        # the mark bar, a field to type in) what would then be refused.
        self.access = FULL_ACCESS
        self.signature_image = None  # SignatureImage the picture-signature tool places
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
        bar = _MarkBar(self._container, self.highlight_color)
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
    # Repaint only the pages whose matches changed: on a remote desktop every repaint
    # is re-encoded and sent.

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
            edges = self._edges_of(page, widget, pos) if widget is not None else ""
            page.setCursor(_EDGE_CURSORS[edges] if edges else
                           Qt.SizeAllCursor if widget is not None else Qt.ArrowCursor)
            return
        if self._tool is not Tool.SELECT:
            return
        edge = self._resize_hit(page, pos) if self.access.annotate else None
        if edge is not None:
            page.setCursor(_EDGE_CURSORS[edge[1]])
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

    # -- drawing shapes, lines, ink; placing stamps

    def _drawn_points(self, page: "PageWidget", drag):
        """(start, end) in widget px, Shift-constrained: equal sides for
        rectangles and ellipses, 45-degree steps for lines and arrows."""
        a, b = drag["press"], drag["end"]
        ax, ay, bx, by = a.x(), a.y(), b.x(), b.y()
        if drag["shift"]:
            dx, dy = bx - ax, by - ay
            if self._tool in (Tool.RECT, Tool.ELLIPSE):
                side = max(abs(dx), abs(dy))
                bx, by = ax + math.copysign(side, dx or 1), ay + math.copysign(side, dy or 1)
            elif self._tool in (Tool.LINE, Tool.ARROW):
                length = math.hypot(dx, dy)
                angle = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
                bx, by = ax + length * math.cos(angle), ay + length * math.sin(angle)
        return (ax, ay), (bx, by)

    def _finish_drawing(self, page: "PageWidget", drag) -> None:
        doc = self.document
        t = page.widget_transform()
        (ax, ay), (bx, by) = self._drawn_points(page, drag)
        color, width = self.shape_color, self.shape_width
        if self._tool is Tool.PEN:
            flat = tuple(v for pt in drag["points"] for v in pt)
            if len(flat) == 2:
                flat = flat * 2  # a dot
            annot = new_ink(flat, self.author, color, width)
            label = "Add Drawing"
        elif math.hypot(bx - ax, by - ay) < 3:
            return  # a click, not a drag
        elif self._tool in (Tool.RECT, Tool.ELLIPSE):
            rect = t.widget_rect_to_pdf((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
            kind = "Square" if self._tool is Tool.RECT else "Circle"
            annot = new_shape(kind, rect, self.author, color, width)
            label = "Add Rectangle" if kind == "Square" else "Add Ellipse"
        else:
            x1, y1 = t.widget_to_pdf(ax, ay)
            x2, y2 = t.widget_to_pdf(bx, by)
            arrow = self._tool is Tool.ARROW
            annot = new_line(x1, y1, x2, y2, self.author, color, width, arrow)
            label = "Add Arrow" if arrow else "Add Line"
        self.annots_edit.emit(doc.page_key(page.index), doc.page_annots(page.index) + (annot,),
                              label)
        if self._tool is not Tool.PEN:
            # One shape per pick (user's preference), selected so it can be
            # nudged or resized straight away. The pen stays: handwriting and
            # signatures take many strokes.
            self.select_annot(page.index, annot.ident)
            self.set_tool(Tool.SELECT)

    def _paint_drawing(self, page: "PageWidget", painter: QPainter, drag) -> None:
        """Preview while dragging -- chrome; pdfium draws the real thing."""
        scale = page.widget_transform().scale
        r, g, b = (round(c * 255) for c in self.shape_color)
        pen = QPen(QColor(r, g, b, 200), max(1.0, self.shape_width * scale))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if self._tool is Tool.PEN:
            path = QPainterPath()
            for i, pt in enumerate(drag["points"]):
                x, y = page.widget_transform().pdf_to_widget(*pt)
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            painter.drawPath(path)
            return
        (ax, ay), (bx, by) = self._drawn_points(page, drag)
        if self._tool is Tool.RECT:
            painter.drawRect(QRectF(min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)))
        elif self._tool is Tool.ELLIPSE:
            painter.drawEllipse(QRectF(min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)))
        else:
            painter.drawLine(QPointF(ax, ay), QPointF(bx, by))

    def _place_stamp(self, page: "PageWidget", pos: QPoint) -> None:
        label, color, dated, name = self.stamp
        sublabel = f"{self.author}, {time.strftime('%Y-%m-%d %H:%M')}" if dated else ""
        x, y = page.pdf_point_at(pos)
        doc = self.document
        stamp = new_stamp(label, sublabel, x, y, self.author, color,
                          doc.page_rotation(page.index), name)
        self.annots_edit.emit(doc.page_key(page.index), doc.page_annots(page.index) + (stamp,),
                              "Add Stamp")
        self.select_annot(page.index, stamp.ident)
        self.set_tool(Tool.SELECT)  # like a note: one stamp per pick

    # -- signatures: drag a box, or click for a standard size

    def _finish_sign_box(self, page: "PageWidget", drag) -> None:
        doc = self.document
        rotation = doc.page_rotation(page.index)
        a, b = drag["press"], drag["end"]
        tool = self._tool
        if b is not None and abs(b.x() - a.x()) >= 8 and abs(b.y() - a.y()) >= 8:
            rect = page.widget_transform().widget_rect_to_pdf(
                (min(a.x(), b.x()), min(a.y(), b.y()), max(a.x(), b.x()), max(a.y(), b.y())))
        else:
            x, y = page.pdf_point_at(a)
            if tool is Tool.SIGNATURE:
                rect = signature_click_box(self.signature_image, x, y, rotation)
            else:
                w, h = SIGN_BOX if rotation % 180 == 0 else SIGN_BOX[::-1]
                rect = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
        self.set_tool(Tool.SELECT)  # one signature per pick, like a stamp
        if tool is Tool.SIGNATURE:
            signature = new_signature_image(self.signature_image, rect, self.author, rotation)
            self.annots_edit.emit(doc.page_key(page.index),
                                  doc.page_annots(page.index) + (signature,), "Add Signature")
            self.select_annot(page.index, signature.ident)
        else:
            self.signature_box.emit(page.index, rect)

    # -- resizing text boxes and shapes (widget space; see _resize_hit)

    def _resize_hit(self, page: "PageWidget", pos: QPoint):
        """(annot, edges) if `pos` is on a resizable edge, else None.

        Only text boxes that read upright on screen: with the view or the
        page turned since typing, "drag the right edge wider" would change
        the text's height instead, which is just confusing.
        """
        if self.document is None:
            return None
        grip = 5  # screen px either side of an edge
        x, y = pos.x(), pos.y()
        page_rotation = self.document.page_rotation(page.index)
        for annot in reversed(self.document.page_annots(page.index)):
            upright_text = annot.kind == "FreeText" and self._rotation == 0 \
                and annot.text_rotation == page_rotation
            if not upright_text and annot.kind not in RESIZABLE_SHAPES:
                continue
            left, top, right, bottom = page.widget_transform().pdf_rect_to_widget(annot.rect)
            if not (left - grip <= x <= right + grip and top - grip <= y <= bottom + grip):
                continue
            edges = ""
            if abs(x - left) <= grip:
                edges += "l"
            elif abs(x - right) <= grip:
                edges += "r"
            if abs(y - top) <= grip:
                edges += "t"
            elif abs(y - bottom) <= grip:
                edges += "b"
            if edges:
                return annot, edges
        return None

    def _resized_rect(self, page: "PageWidget", drag, pos: QPoint):
        """The dragged box in widget px, never narrower than a character or
        shorter than its text needs at that width -- text is never cut off."""
        annot, edges = drag["annot"], drag["edges"]
        left, top, right, bottom = drag["start"]
        dx, dy = pos.x() - drag["press"].x(), pos.y() - drag["press"].y()
        if "l" in edges:
            left += dx
        if "r" in edges:
            right += dx
        if "t" in edges:
            top += dy
        if "b" in edges:
            bottom += dy
        scale = page.widget_transform().scale
        if annot.kind != "FreeText":
            # Shapes: anything but degenerate. Edges may cross; normalise.
            min_side = 4.0
            if right - left < min_side:
                if "l" in edges:
                    left = right - min_side
                else:
                    right = left + min_side
            if bottom - top < min_side:
                if "t" in edges:
                    top = bottom - min_side
                else:
                    bottom = top + min_side
            return left, top, right, bottom
        min_width = (annot.font_size + 2 * PADDING) * scale
        if right - left < min_width:
            if "l" in edges:
                left = right - min_width
            else:
                right = left + min_width
        needed = layout(annot.contents, annot.font_size, (right - left) / scale).height * scale
        if bottom - top < needed:
            if "t" in edges:
                top = bottom - needed
            else:
                bottom = top + needed
        return left, top, right, bottom

    # -- text boxes: typed in place, committed as a FreeText annotation

    def is_editing(self) -> bool:
        return self._editor is not None or self._field_editor is not None

    # -- form fields: filled in place, committed as FieldEdits

    def _widget_at(self, page: "PageWidget", pos: QPoint, fillable_only: bool = True):
        doc = self.document
        if doc is None:
            return None
        x, y = page.pdf_point_at(pos)
        for widget in reversed(doc.current_widgets(page.index)):
            if widget.contains(x, y) and widget.kind in (TEXT, CHECKBOX, RADIO, COMBO):
                if widget.fillable or not fillable_only:
                    return widget
        return None

    # -- preparing a form: fields drawn, moved, resized, deleted

    def _field_at(self, page: "PageWidget", pos: QPoint) -> Optional[Widget]:
        x, y = page.pdf_point_at(pos)
        grip = 3.0 / max(page.widget_transform().scale, 1e-6)
        for widget in reversed(self.document.current_widgets(page.index)):
            x0, y0, x1, y1 = widget.rect
            if x0 - grip <= x <= x1 + grip and y0 - grip <= y <= y1 + grip:
                return widget
        return None

    def selected_field(self):
        """(page index, Widget) of the field selected in Edit Fields, or None."""
        if self._selected_field is None or self.document is None:
            return None
        key, ident = self._selected_field
        for page in self._pages:
            if self.document.page_key(page.index) == key:
                for widget in self.document.current_widgets(page.index):
                    if widget.ident == ident:
                        return page.index, widget
        return None

    def select_field(self, index: int, ident) -> None:
        self._selected_field = (self.document.page_key(index), ident) if ident is not None else None
        for page in self._pages:
            page.update()

    def _edges_of(self, page: "PageWidget", widget: Widget, pos: QPoint) -> str:
        grip = 5
        left, top, right, bottom = page.widget_transform().pdf_rect_to_widget(widget.rect)
        x, y = pos.x(), pos.y()
        if not (left - grip <= x <= right + grip and top - grip <= y <= bottom + grip):
            return ""
        edges = ""
        if abs(x - left) <= grip:
            edges += "l"
        elif abs(x - right) <= grip:
            edges += "r"
        if abs(y - top) <= grip:
            edges += "t"
        elif abs(y - bottom) <= grip:
            edges += "b"
        return edges

    def _form_press(self, page: "PageWidget", pos: QPoint) -> bool:
        widget = self._field_at(page, pos)
        if widget is None:
            # Clicking away from the fields means done preparing the form
            # (user's rule, as for text boxes and shapes): back to Select.
            self.select_field(page.index, None)
            self.set_tool(Tool.SELECT)
            return True
        self.select_field(page.index, widget.ident)
        edges = self._edges_of(page, widget, pos)
        start = page.widget_transform().pdf_rect_to_widget(widget.rect)
        self._drag = {"mode": "fresize" if edges else "fmove", "page": page, "widget": widget,
                      "edges": edges, "press": pos, "start": start, "end": None}
        return True

    def _dragged_field_rect(self, page: "PageWidget", drag):
        """The field's new rect in widget px while moving / resizing."""
        left, top, right, bottom = drag["start"]
        dx = drag["end"].x() - drag["press"].x()
        dy = drag["end"].y() - drag["press"].y()
        if drag["mode"] == "fmove":
            return left + dx, top + dy, right + dx, bottom + dy
        edges = drag["edges"]
        if "l" in edges:
            left = min(left + dx, right - 6)
        if "r" in edges:
            right = max(right + dx, left + 6)
        if "t" in edges:
            top = min(top + dy, bottom - 6)
        if "b" in edges:
            bottom = max(bottom + dy, top + 6)
        return left, top, right, bottom

    def _commit_field_geometry(self, page: "PageWidget", drag) -> None:
        doc = self.document
        rect = page.widget_transform().widget_rect_to_pdf(self._dragged_field_rect(page, drag))
        old = drag["widget"]
        widgets = tuple(w.moved_to(rect) if w.ident == old.ident else w
                        for w in doc.current_widgets(page.index))
        verb = "Move" if drag["mode"] == "fmove" else "Resize"
        self.forms_edit.emit(doc.page_key(page.index), widgets, f"{verb} {old.short_name()}")

    def taken_field_names(self, file: int) -> set:
        doc = self.document
        return {w.name for i in range(doc.page_count) if doc.pages[i].file == file
                for w in doc.current_widgets(i)}

    def _create_field(self, page: "PageWidget", drag) -> None:
        doc = self.document
        kind, multiline = FIELD_TOOLS[self._tool]
        t = page.widget_transform()
        a, b = drag["press"], drag["end"]
        if b is not None and abs(b.x() - a.x()) >= 6 and abs(b.y() - a.y()) >= 6:
            rect = t.widget_rect_to_pdf((min(a.x(), b.x()), min(a.y(), b.y()),
                                         max(a.x(), b.x()), max(a.y(), b.y())))
        else:
            # A click: standard size, top-left at the click.
            w, h = NEW_FIELD_SIZES[kind]
            if multiline:
                h = 60.0
            x, y = page.pdf_point_at(a)
            rect = (x, y - h, x + w, y)
        name = unique_name(kind, self.taken_field_names(doc.pages[page.index].file))
        options = ("Option 1", "Option 2", "Option 3") if kind == COMBO else ()
        field = new_field(kind, name, rect, multiline, options)
        self.forms_edit.emit(doc.page_key(page.index), doc.current_widgets(page.index) + (field,),
                             f"Add {name}")
        self.set_tool(Tool.FORM)
        self.select_field(page.index, field.ident)
        if kind == COMBO:
            self.field_properties.emit(page.index, field)  # a dropdown needs its options

    def delete_selected_field(self) -> bool:
        if self._tool is not Tool.FORM:
            return False
        found = self.selected_field()
        if found is None:
            return False
        index, gone = found
        doc = self.document
        self._selected_field = None
        self.forms_edit.emit(doc.page_key(index),
                             tuple(w for w in doc.current_widgets(index) if w.ident != gone.ident),
                             f"Delete {gone.short_name()}")
        return True

    def _paint_fields(self, page: "PageWidget", painter: QPainter) -> None:
        """Preparing a form: every field outlined and named, the selected
        one solid, the one being dragged or drawn previewed. Chrome only."""
        doc = self.document
        selected = self.selected_field()
        drag = self._drag
        small = QFont("Arial")
        small.setPixelSize(10)
        painter.setFont(small)
        for widget in doc.current_widgets(page.index):
            box = self._widget_rect(page, widget.rect)
            is_selected = selected is not None and selected[0] == page.index \
                and selected[1].ident == widget.ident
            painter.setPen(QPen(QColor(40, 110, 230) if is_selected else QColor(120, 120, 140),
                                2 if is_selected else 1, Qt.SolidLine if is_selected else Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(box)
            painter.setPen(QColor(40, 110, 230) if is_selected else QColor(90, 90, 110))
            painter.drawText(QPointF(box.left() + 2, box.top() - 3), widget.short_name())
        if drag is not None and drag["page"] is page and drag.get("end") is not None:
            if drag["mode"] in ("fmove", "fresize"):
                left, top, right, bottom = self._dragged_field_rect(page, drag)
                box = QRectF(left, top, right - left, bottom - top)
            elif drag["mode"] == "fdraw":
                a, b = drag["press"], drag["end"]
                box = QRectF(QPointF(min(a.x(), b.x()), min(a.y(), b.y())),
                             QPointF(max(a.x(), b.x()), max(a.y(), b.y())))
            else:
                return
            painter.fillRect(box, QColor(40, 110, 230, 40))
            painter.setPen(QPen(QColor(40, 110, 230), 1.5, Qt.DashLine))
            painter.drawRect(box)

    def _field_key(self, index: int, widget: Widget):
        return (self.document.pages[index].file, widget.name)

    def _fill(self, page: "PageWidget", widget: Widget) -> None:
        doc = self.document
        if not widget.fillable:
            self.message.emit(f"\u201c{widget.short_name()}\u201d is read-only.")
            return
        file = doc.pages[page.index].file
        key = self._field_key(page.index, widget)
        if widget.kind == TEXT:
            self._open_field(page, widget)
        elif widget.kind == CHECKBOX:
            on = is_on(widget, file, doc.fields)
            self.fields_edit.emit(key, "Off" if on else widget.on_state,
                                  f"{'Uncheck' if on else 'Check'} {widget.short_name()}")
        elif widget.kind == RADIO:
            if not is_on(widget, file, doc.fields):
                self.fields_edit.emit(key, widget.on_state,
                                      f"Choose {widget.on_state} for {widget.short_name()}")
        elif widget.kind == COMBO and widget.options:
            menu = QMenu(self)
            current = current_value(widget, file, doc.fields)
            for option in widget.options:
                act = menu.addAction(option)
                act.setCheckable(True)
                act.setChecked(option == current)
            left, top, right, bottom = page.widget_transform().pdf_rect_to_widget(widget.rect)
            chosen = menu.exec(page.mapToGlobal(QPoint(round(left), round(bottom))))
            if chosen is not None and chosen.text() != current:
                self.fields_edit.emit(key, chosen.text(), f"Choose {widget.short_name()}")

    def _open_field(self, page: "PageWidget", widget: Widget) -> None:
        self.commit_text_edit()
        self.commit_field_edit()
        doc = self.document
        file = doc.pages[page.index].file
        value = current_value(widget, file, doc.fields)
        left, top, right, bottom = page.widget_transform().pdf_rect_to_widget(widget.rect)
        scale = page.widget_transform().scale
        x0, y0, x1, y1 = widget.rect
        size = widget.font_size or auto_font_size(value, x1 - x0, y1 - y0, widget.multiline)
        editor = _FieldEditor(page, widget.multiline)
        font = QFont("Arial")  # Helvetica's metric twin, as the saved field uses
        font.setPixelSize(max(1, round(size * scale)))
        editor.setFont(font)
        editor.document().setDocumentMargin(max(1.0, 1.5 * scale))
        editor.setStyleSheet("QPlainTextEdit { background: white; color: black;"
                             " border: 2px solid rgb(40,110,230); }")
        editor.setPlainText(value)
        editor.setGeometry(round(left), round(top), max(12, round(right - left)),
                           max(12, round(bottom - top)))
        self._field_editor = {"page": page, "widget": widget, "editor": editor, "value": value}
        editor.finished.connect(self._field_finished)
        editor.show()
        editor.setFocus()
        editor.selectAll()
        self.editing_changed.emit(True)

    def _field_finished(self, move: int) -> None:
        ed = self._field_editor
        if ed is None:
            return
        page, widget = ed["page"], ed["widget"]
        if not self.commit_field_edit() or move == 0:
            return
        # Tab / Shift+Tab: the next or previous fillable text field, in
        # page order and then the order the form lists them.
        order = [(p.index, w) for p in self._pages
                 for w in self.document.current_widgets(p.index) if w.kind == TEXT and w.fillable]
        here = next((n for n, (i, w) in enumerate(order)
                     if i == page.index and w.origin == widget.origin), None)
        if here is None or not order:
            return
        index, nxt = order[(here + move) % len(order)]
        if index != page.index:
            self.scroll_to_page(index)
        self._open_field(self._pages[index], nxt)

    def commit_field_edit(self) -> bool:
        """Close the field editor, turning what was typed into a value.
        False if it stayed open (text the field's font can't hold)."""
        ed = self._field_editor
        if ed is None:
            return True
        editor, widget, page = ed["editor"], ed["widget"], ed["page"]
        text = editor.toPlainText()
        if not widget.multiline:
            text = text.replace("\n", " ")
        if not encodable(text):
            self.message.emit("Only Western European characters can be typed into this "
                              "field for now; please remove the others.")
            editor.reopen()
            return False
        self._field_editor = None
        editor.hide()
        editor.deleteLater()
        self.setFocus(Qt.OtherFocusReason)
        self.editing_changed.emit(False)
        if self.document is not None and page.document is self.document and text != ed["value"]:
            self.fields_edit.emit(self._field_key(page.index, widget), text,
                                  f"Fill {widget.short_name()}")
        return True

    def _new_text(self, page: "PageWidget", pos: QPoint, boxed: bool, width) -> None:
        self._open_editor(page, pos, None, "", self.text_size, self.text_color, boxed, width)

    def _edit_text(self, page: "PageWidget", annot: Annot) -> None:
        left, top, _, _ = page.widget_transform().pdf_rect_to_widget(annot.rect)
        width = text_box_size(annot)[0] if annot.fixed_width else None
        self.select_annot(page.index, annot.ident)
        self._open_editor(page, QPoint(round(left), round(top)), annot, annot.contents,
                          annot.font_size, annot.color, annot.boxed, width)

    def _open_editor(self, page, anchor: QPoint, annot, text, size, color, boxed, width) -> None:
        self.commit_text_edit()
        editor = _TextEditor(page)
        editor.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if width is not None else QPlainTextEdit.NoWrap)
        editor.setPlainText(text)
        bar = _StyleBar(self._container, size, color)
        self._editor = {"page": page, "widget": editor, "annot": annot, "anchor": anchor,
                        "size": size, "color": color, "boxed": boxed, "width": width,
                        "bar": bar}
        self._style_editor()
        editor.textChanged.connect(self._fit_editor)
        editor.finished.connect(self.commit_text_edit)
        bar.chosen.connect(self._restyle_text)
        self._fit_editor()
        bar.show()
        editor.show()
        editor.setFocus()
        editor.moveCursor(QTextCursor.End)
        self.editing_changed.emit(True)

    def _style_editor(self) -> None:
        """Font and colour of the open editor, from its size and colour."""
        ed = self._editor
        scale = ed["page"].widget_transform().scale
        font = QFont("Arial")  # Helvetica's metric twin: wraps where the PDF will
        font.setPixelSize(max(1, round(ed["size"] * scale)))
        ed["widget"].setFont(font)
        ed["widget"].document().setDocumentMargin(PADDING * scale)
        r, g, b = (round(c * 255) for c in ed["color"])
        ed["widget"].setStyleSheet(
            f"QPlainTextEdit {{ color: rgb({r},{g},{b}); background: rgba(255,255,255,215);"
            f" border: 1px dashed rgb(40,110,230); }}")

    def _restyle_text(self, size, color) -> None:
        """A pick on the style bar: restyle what's being typed, and make it
        the default for the next text."""
        ed = self._editor
        if ed is None:
            return
        if size is not None:
            ed["size"] = self.text_size = size
        if color is not None:
            ed["color"] = self.text_color = color
        self._style_editor()
        self._fit_editor()
        self.text_style_changed.emit()

    def _fit_editor(self) -> None:
        ed = self._editor
        if ed is None:
            return
        page = ed["page"]
        scale = page.widget_transform().scale
        lay = layout(ed["widget"].toPlainText(), ed["size"], ed["width"])
        # A little extra room: Qt's line spacing differs slightly from ours,
        # and the committed box is sized by layout(), not by this widget.
        ed["widget"].setGeometry(ed["anchor"].x(), ed["anchor"].y(),
                                 math.ceil(lay.width * scale) + 4,
                                 math.ceil(lay.height * scale * 1.05) + 4)
        # The style bar sits just above the text, or below it at a page's
        # top edge; the text grows downward, so re-place it every time.
        bar = ed["bar"]
        box = ed["widget"].geometry().translated(page.pos())
        y = box.top() - bar.height() - 4
        if y < page.y() + 4:
            y = box.bottom() + 5
        bar.move(*self._clamped(bar, box.left(), y))
        bar.raise_()

    def commit_text_edit(self) -> None:
        """Close the text editor, turning what was typed into an edit."""
        ed = self._editor
        if ed is None:
            return
        widget, page = ed["widget"], ed["page"]
        text = widget.toPlainText().rstrip()
        if not encodable(text):
            # Refuse rather than save question marks. The editor stays open.
            self.message.emit("Only Western European characters can be typed into PDF text "
                              "boxes for now; please remove the others.")
            widget.reopen()
            return
        self._editor = None
        widget.hide()
        widget.deleteLater()
        ed["bar"].hide()
        ed["bar"].deleteLater()
        self.setFocus(Qt.OtherFocusReason)
        self.editing_changed.emit(False)
        # However typing ended -- click away, Esc, Ctrl+Enter -- it ends the
        # text tool too (the user asked for this; an idle click shouldn't
        # spawn a box).
        if self._tool in TEXT_TOOLS:
            self.set_tool(Tool.SELECT)

        index = page.index
        if self.document is None or page.document is not self.document                 or index >= self.document.page_count:
            return  # the page it was typed on is gone; nothing to attach it to
        scale = page.widget_transform().scale
        lay = layout(text, ed["size"], ed["width"])
        ax, ay = ed["anchor"].x(), ed["anchor"].y()
        rect = page.widget_transform().widget_rect_to_pdf(
            (ax, ay, ax + lay.width * scale, ay + lay.height * scale))
        key = self.document.page_key(index)
        annots = self.document.page_annots(index)
        old = ed["annot"]
        if old is None:
            if text.strip():
                new = new_text(rect, text, self.author, ed["size"], ed["color"], ed["boxed"],
                               self.document.page_rotation(index))
                self.annots_edit.emit(key, annots + (new,),
                                      "Add Text Box" if ed["boxed"] else "Add Text")
                self.select_annot(index, new.ident)
        elif not text.strip():
            self.annots_edit.emit(key, without(annots, old), "Delete Text")
        elif (text, ed["size"], tuple(ed["color"])) != \
                (old.contents, old.font_size, tuple(old.color)):
            # A new colour or size alone is an edit too (the style bar).
            changed = replace(old, contents=text, rect=rect, font_size=ed["size"],
                              color=ed["color"], edited=True, reshaped=True)
            self.annots_edit.emit(key, tuple(changed if a.ident == old.ident else a
                                             for a in annots), "Edit Text")

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
