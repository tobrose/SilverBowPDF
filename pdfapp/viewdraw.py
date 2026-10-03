"""Drawing on pages: shapes, lines, ink, stamps, signature boxes."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from .annots import (
    new_ink,
    new_line,
    new_shape,
    new_stamp,
    new_signature_image,
    signature_click_box,
)
from .tools import Tool, SIGN_BOX


class DrawingMixin:
    """PdfView: drawing shapes, lines and ink, placing stamps and signature boxes."""

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
