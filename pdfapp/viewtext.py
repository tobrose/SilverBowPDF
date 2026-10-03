"""Text boxes on pages: typed in place, restyled, resized."""

from __future__ import annotations

import math
from dataclasses import replace

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

from .annots import Annot, new_text, text_box_size, without
from .textlayout import PADDING, encodable, layout
from .tools import Tool, TEXT_TOOLS, RESIZABLE_SHAPES
from .viewbars import TextEditor, StyleBar


class TextBoxMixin:
    """PdfView: text boxes typed in place, and resizing text boxes and shapes."""

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
        editor = TextEditor(page)
        editor.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if width is not None else QPlainTextEdit.NoWrap)
        editor.setPlainText(text)
        bar = StyleBar(self._container, size, color)
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
