"""Tools and fit modes of the page view, and which tools behave alike.

Shared by the view (pageview.py), its mixins (viewdraw, viewtext,
viewforms) and MainWindow."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt

from .fields import CHECKBOX, COMBO, TEXT


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

EDGE_CURSORS = {
    "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
    "lt": Qt.SizeFDiagCursor, "rb": Qt.SizeFDiagCursor, "rt": Qt.SizeBDiagCursor,
    "lb": Qt.SizeBDiagCursor,
}
