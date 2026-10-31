"""Small widgets the page view floats over pages: the in-place editors for
text boxes and form fields, the mark bar under a text selection, and the
typewriter's style bar."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QToolButton,
)

from .annots import HIGHLIGHT_COLORS, TEXT_COLORS, TEXT_SIZES


class FieldEditor(QPlainTextEdit):
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


class TextEditor(QPlainTextEdit):
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


def swatch_icon(color, size: int = 12) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor.fromRgbF(*color))
    return QIcon(pixmap)


class FloatingBar(QFrame):
    """A small row of buttons floating by the text: the mark bar under a
    selection, the style bar over the typewriter. A child of the scrolling
    page column, not a popup window -- it scrolls with the page, can't grab
    the next click, and costs a remote desktop one small repaint -- and not
    of the page itself, which would clip it when zoomed out narrower than
    the bar. Its buttons never take focus, so keys stay with the view, and a
    text editor being typed in keeps its caret."""

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


class MarkBar(FloatingBar):
    """Under a fresh text selection (Select tool): Highlight in the current
    colour or one of the others, Underline, Strikethrough."""

    chosen = Signal(str, object)  # kind, highlight colour (None: current / not a highlight)

    def __init__(self, parent, highlight_color):
        super().__init__(parent)
        self.button("Highlight", swatch_icon(highlight_color),
                    "Highlight in the current colour").clicked.connect(
            lambda: self.chosen.emit("Highlight", None))
        for name, color in HIGHLIGHT_COLORS.items():
            self.button(icon=swatch_icon(color), tip=f"Highlight {name.lower()}").clicked.connect(
                lambda _=False, c=color: self.chosen.emit("Highlight", c))
        self.gap()
        self.button("Underline").clicked.connect(lambda: self.chosen.emit("Underline", None))
        self.button("Strikethrough").clicked.connect(lambda: self.chosen.emit("StrikeOut", None))
        self.adjustSize()


class StyleBar(FloatingBar):
    """Over the typewriter while typing: text colour and size. Changing
    them restyles what's being typed, and becomes the default for the next."""

    chosen = Signal(object, object)  # size or None, colour or None

    def __init__(self, parent, size: float, color):
        super().__init__(parent)
        colors, sizes = QButtonGroup(self), QButtonGroup(self)  # one ticked in each
        self.label("Colour")
        for name, c in TEXT_COLORS.items():
            b = self.button(icon=swatch_icon(c, 14), tip=name, checked=tuple(c) == tuple(color))
            colors.addButton(b)
            b.clicked.connect(lambda _=False, c=c: self.chosen.emit(None, c))
        self.gap()
        self.label("Size")
        for s in TEXT_SIZES:
            b = self.button(str(s), tip=f"{s} pt", checked=abs(s - size) < 0.01)
            sizes.addButton(b)
            b.clicked.connect(lambda _=False, s=s: self.chosen.emit(float(s), None))
        self.adjustSize()
