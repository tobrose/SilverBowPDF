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
from dataclasses import replace
from enum import Enum
from typing import List, Optional

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextCursor, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .annots import (
    HIGHLIGHT_COLORS,
    TEXT_BOX_WIDTH,
    TEXT_COLORS,
    Annot,
    describe,
    lay_markup,
    new_markup,
    new_text,
    text_box_size,
    without,
)
from .textlayout import PADDING, encodable, layout
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
    HIGHLIGHT = "highlight"
    UNDERLINE = "underline"
    STRIKEOUT = "strikeout"
    TYPEWRITER = "typewriter"
    TEXTBOX = "textbox"


# Tools that mark text: drag across it, a markup annotation lands on release.
MARKUP_TOOLS = {Tool.HIGHLIGHT: "Highlight", Tool.UNDERLINE: "Underline",
                Tool.STRIKEOUT: "StrikeOut"}
TEXT_TOOLS = (Tool.TYPEWRITER, Tool.TEXTBOX)


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
    editing_changed = Signal(bool)             # a text box editor is open
    message = Signal(str)                      # something the user should know

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
        self._drag = None       # in-progress mouse drag, see page_mouse_press
        self._text_sel = None   # (page index, anchor char, focus char)
        self.highlight_color = HIGHLIGHT_COLORS["Yellow"]
        self.text_size = 12.0
        self.text_color = TEXT_COLORS["Black"]
        self._editor = None     # open text box editor, see _open_editor
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
        for page in self._pages:
            page.setCursor(self._tool_cursor())
        self.tool_changed.emit(tool)

    def _tool_cursor(self):
        if self._tool is Tool.NOTE:
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
            self._update_pages({index} | ({old[0]} if old else set()))
            if old is None:
                self.text_selection_changed.emit(True)

    def clear_text_selection(self) -> None:
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

    def page_mouse_press(self, page: "PageWidget", event) -> bool:
        if event.button() != Qt.LeftButton or self.document is None:
            return False
        if self._editor is not None:
            # A click away from the text you're typing means "done": finish
            # it (which also leaves the text tool) and do nothing else --
            # not start another box, not select what's under the click.
            self.commit_text_edit()
            return True
        self.setFocus(Qt.MouseFocusReason)
        pos = event.position().toPoint()
        if self._tool is Tool.NOTE:
            x, y = page.pdf_point_at(pos)
            self.note_requested.emit(page.index, x, y)
            return True
        if self._tool in TEXT_TOOLS:
            hit = self._annot_at(page, pos)
            if hit is not None and hit.kind == "FreeText":
                self._edit_text(page, hit)  # the typewriter clicks into existing text
                return True
            self._drag = {"mode": "place", "page": page, "press": pos, "end": None}
            return True
        annot = None if self._tool in MARKUP_TOOLS else self._annot_at(page, pos)
        self.select_annot(page.index, annot.ident if annot else None)
        if annot is not None and not annot.is_markup:
            self.clear_text_selection()
            self._drag = {"mode": "move", "page": page, "annot": annot, "press": pos,
                          "start": page.pdf_point_at(pos), "delta": None}
        else:
            self._drag = {"mode": "text", "page": page, "press": pos, "anchor": None,
                          "annot": annot}
        return True

    def page_mouse_move(self, page: "PageWidget", event) -> bool:
        if self.document is None:
            return False
        pos = event.position().toPoint()
        drag = self._drag
        if drag is not None and event.buttons() & Qt.LeftButton and drag["page"] is page:
            started = drag.get("delta") is not None or drag.get("anchor") is not None \
                or drag.get("end") is not None
            if not started and \
                    (pos - drag["press"]).manhattanLength() < QApplication.startDragDistance():
                return True
            if drag["mode"] == "place":
                if self._tool is Tool.TEXTBOX:
                    drag["end"] = pos
                    page.update()
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
        if self._tool is not Tool.SELECT:
            return
        annot = self._annot_at(page, pos)
        if annot is not None and not annot.is_markup:
            page.setCursor(Qt.SizeAllCursor)
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
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.LeftButton:
            return False
        if drag["mode"] == "place":
            press = drag["press"]
            if self._tool is Tool.TEXTBOX:
                scale = page.widget_transform().scale
                end = drag["end"]
                if end is not None and abs(end.x() - press.x()) / scale >= 40:
                    width = abs(end.x() - press.x()) / scale
                    press = QPoint(min(end.x(), press.x()), press.y())
                else:
                    width = TEXT_BOX_WIDTH
                self._new_text(page, press, boxed=True, width=width)
            else:
                self._new_text(page, press, boxed=False, width=None)
            page.update()
            return True
        if drag["mode"] == "move":
            if drag["delta"] is not None:
                dx, dy = drag["delta"]
                moved = drag["annot"]
                annots = tuple(a.moved(dx, dy) if a.ident == moved.ident else a
                               for a in self.document.page_annots(page.index))
                self.annots_edit.emit(self.document.page_key(page.index), annots,
                                      f"Move {describe(moved.kind)}")
        elif drag["anchor"] is None:
            self.clear_text_selection()  # a click, not a drag
        elif self._tool in MARKUP_TOOLS:
            self.mark_selection(MARKUP_TOOLS[self._tool])
        page.update()
        return True

    def page_double_click(self, page: "PageWidget", event) -> bool:
        if self._tool is not Tool.SELECT or self.document is None:
            return False
        pos = event.position().toPoint()
        annot = self._annot_at(page, pos)
        if annot is not None and annot.kind == "FreeText":
            self._edit_text(page, annot)
            return True
        if annot is not None:
            self.annot_open.emit(page.index, annot)
            return True
        char = self._char_near(page, pos, exact=True)
        if char >= 0:
            self.set_text_selection(page.index, *self.document.word_at(page.index, char))
            return True
        return False

    # -- text boxes: typed in place, committed as a FreeText annotation

    def is_editing(self) -> bool:
        return self._editor is not None

    def _new_text(self, page: "PageWidget", pos: QPoint, boxed: bool, width) -> None:
        self._open_editor(page, pos, None, "", self.text_size, self.text_color, boxed, width)

    def _edit_text(self, page: "PageWidget", annot: Annot) -> None:
        left, top, _, _ = page.widget_transform().pdf_rect_to_widget(annot.rect)
        width = text_box_size(annot)[0] if annot.boxed else None
        self.select_annot(page.index, annot.ident)
        self._open_editor(page, QPoint(round(left), round(top)), annot, annot.contents,
                          annot.font_size, annot.color, annot.boxed, width)

    def _open_editor(self, page, anchor: QPoint, annot, text, size, color, boxed, width) -> None:
        self.commit_text_edit()
        if self._rotation != 0:
            # Qt can't draw the editor sideways, and text typed while the view
            # is turned would come out sideways once it's turned back.
            self.message.emit("Rotate the view back to normal (Ctrl+L / Ctrl+R) to type text.")
            return
        scale = page.widget_transform().scale
        editor = _TextEditor(page)
        font = QFont("Arial")  # Helvetica's metric twin: wraps where the PDF will
        font.setPixelSize(max(1, round(size * scale)))
        editor.setFont(font)
        editor.document().setDocumentMargin(PADDING * scale)
        r, g, b = (round(c * 255) for c in color)
        editor.setStyleSheet(
            f"QPlainTextEdit {{ color: rgb({r},{g},{b}); background: rgba(255,255,255,215);"
            f" border: 1px dashed rgb(40,110,230); }}")
        editor.setLineWrapMode(QPlainTextEdit.WidgetWidth if boxed else QPlainTextEdit.NoWrap)
        editor.setPlainText(text)
        self._editor = {"page": page, "widget": editor, "annot": annot, "anchor": anchor,
                        "size": size, "color": color, "boxed": boxed, "width": width}
        editor.textChanged.connect(self._fit_editor)
        editor.finished.connect(self.commit_text_edit)
        self._fit_editor()
        editor.show()
        editor.setFocus()
        editor.moveCursor(QTextCursor.End)
        self.editing_changed.emit(True)

    def _fit_editor(self) -> None:
        ed = self._editor
        if ed is None:
            return
        scale = ed["page"].widget_transform().scale
        lay = layout(ed["widget"].toPlainText(), ed["size"], ed["width"] if ed["boxed"] else None)
        # A little extra room: Qt's line spacing differs slightly from ours,
        # and the committed box is sized by layout(), not by this widget.
        ed["widget"].setGeometry(ed["anchor"].x(), ed["anchor"].y(),
                                 math.ceil(lay.width * scale) + 4,
                                 math.ceil(lay.height * scale * 1.05) + 4)

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
        lay = layout(text, ed["size"], ed["width"] if ed["boxed"] else None)
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
        elif text != old.contents:
            changed = replace(old, contents=text, rect=rect, edited=True, reshaped=True)
            self.annots_edit.emit(key, tuple(changed if a.ident == old.ident else a
                                             for a in annots), "Edit Text")

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
            self.clear_text_selection()
            if self._selected is not None:
                self.select_annot(0, None)
            return
        super().keyPressEvent(event)

    def paint_page_chrome(self, page: "PageWidget", painter: QPainter) -> None:
        """Text selection, and the selected annotation's outline and drag
        preview. UI chrome only -- annotations are always drawn by pdfium."""
        drag = self._drag
        if drag is not None and drag["mode"] == "place" and drag["page"] is page \
                and drag["end"] is not None:
            a, b = drag["press"], drag["end"]
            painter.setPen(QPen(QColor(40, 110, 230), 1, Qt.DashLine))
            painter.drawRect(QRect(QPoint(min(a.x(), b.x()), a.y()),
                                   QPoint(max(a.x(), b.x()), a.y() + 24)))
        if self._text_sel is not None and self._text_sel[0] == page.index:
            _, start, end = self._text_sel
            for rect in self.document.text_rects(page.index, start, end):
                painter.fillRect(self._widget_rect(page, rect), QColor(0, 120, 215, 70))

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
        if self.document is not None:
            self.document.close()
        self.document = document
        self._selected = None
        self._drag = None
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
        self.commit_text_edit()  # the editor is sized for the old zoom

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
        self.commit_text_edit()
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
