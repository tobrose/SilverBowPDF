"""Form fields on pages: filling them, and preparing a form."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QMenu

from .fields import (
    CHECKBOX,
    COMBO,
    NEW_FIELD_SIZES,
    RADIO,
    SIGNATURE,
    TEXT,
    Widget,
    auto_font_size,
    current_value,
    is_on,
    new_field,
    unique_name,
)
from .textlayout import encodable
from .tools import Tool, FIELD_TOOLS
from .viewbars import FieldEditor


class FormsMixin:
    """PdfView: form fields -- filled in place, and drawn, moved, resized and
    deleted while preparing a form (Edit Fields)."""

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

    def _locked(self, widget: Widget) -> bool:
        """A signed signature field: its signature covers where it is, so
        moving, resizing or deleting it breaks the signature (Acrobat
        locks them too). Saving would warn -- this doesn't let it start."""
        return widget.kind == SIGNATURE and (widget.signed or widget.name in self.signed_fields)

    def _form_press(self, page: "PageWidget", pos: QPoint) -> bool:
        widget = self._field_at(page, pos)
        if widget is not None and self._locked(widget):
            self.select_field(page.index, None)
            self.message.emit("This signature field is signed: moving, resizing or deleting it "
                              "would break the signature.")
            return True
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
        editor = FieldEditor(page, widget.multiline)
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
