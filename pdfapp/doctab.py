"""One open document, as a tab of the main window.

Everything that belongs to a single document lives here: its page view, its
find bar, the notice bars above its pages (signatures, author's
restrictions), its page sidebar and its undo history. The menus, toolbars
and status bar are the window's and act on the current tab (MainWindow
reaches these through properties of the same names it always used).

A tab that isn't shown gives back its rendered pages (release_memory): on a
shared multi-session host, five open PDFs must not cost five times the memory.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .annots import DocState
from .document import PdfDocument
from .findbar import FindBar
from .pagelist import History
from .pageview import PdfView
from .thumbnails import ThumbnailSidebar

# The signature bar's colours by level (pdfsign's): background, border.
_SIGNATURE_COLOURS = {"ok": ("#e6f4ea", "#9fd3ae"), "untrusted": ("#fff4ce", "#e0c870"),
                      "changed": ("#ffe7cc", "#e8b070"), "broken": ("#fde7e9", "#e8a0a8")}
# Explicit colours for the bars' buttons as well as their text: in Windows'
# dark mode a button would draw pale text on these pale bars (seen, not
# guessed).
_BUTTON_STYLE = ("QPushButton {{ color: #1e1e1e; background: #ffffff; border: 1px solid {edge};"
                 " border-radius: 3px; padding: 3px 14px; }}")


def _bar(name: str, button_text: str):
    bar = QFrame()
    bar.setObjectName(name)
    row = QHBoxLayout(bar)
    row.setContentsMargins(10, 5, 10, 5)
    text = QLabel(bar)
    text.setWordWrap(True)
    row.addWidget(text, 1)
    button = QPushButton(button_text, bar)
    row.addWidget(button)
    bar.hide()
    return bar, text, button


class DocumentTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = PdfView(self)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.find_bar = FindBar(self.view, self)
        # Lives in the window's dock (a stack, one per tab), not in this widget.
        self.sidebar = ThumbnailSidebar()
        self.sidebar.page_activated.connect(self.view.scroll_to_page)
        self.history: Optional[History[DocState]] = None
        self.signature_infos: list = []
        # A closed document takes its history with it, current tab or not
        # (MainWindow only hears the current tab).
        self.view.document_opened.connect(self._on_document_opened)

        # Says whether the document's digital signatures check out.
        self.signature_bar, self.signature_text, self.details_button = _bar(
            "signatures", "Details…")
        self.signature_text.setTextFormat(Qt.RichText)
        # Says what a restricted document's author has turned off, and offers
        # the owner password -- mouse users need the why, not just grey buttons.
        self.restriction_bar, self.restriction_text, self.unlock_button = _bar(
            "restrictions", "Unlock…")
        self.unlock_button.setToolTip("Enter the owner password to lift the restrictions")
        self.restriction_bar.setStyleSheet(
            "#restrictions { background: #fff4ce; border-bottom: 1px solid #e0c870; }"
            "QLabel { color: #3b3000; }" + _BUTTON_STYLE.format(edge="#b89b2e")
            + "QPushButton:hover { background: #fffbe8; }"
            "QPushButton:pressed { background: #f3e6b0; }")

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        for widget in (self.signature_bar, self.restriction_bar, self.find_bar):
            column.addWidget(widget)
        column.addWidget(self.view, 1)

    @property
    def document(self) -> Optional[PdfDocument]:
        return self.view.document

    @property
    def dirty(self) -> bool:
        return self.document is not None and self.history is not None and self.history.dirty

    def _on_document_opened(self, document) -> None:
        if document is None:
            self.history = None
            self.signature_infos = []
            self.signature_bar.hide()
            self.restriction_bar.hide()

    def title(self) -> str:
        """The tab's label: the file's name, marked while it has unsaved changes."""
        if self.document is None:
            return "Start"
        return self.document.path.name + (" •" if self.dirty else "")

    def show_signature_bar(self, level: str, text: str) -> None:
        background, border = _SIGNATURE_COLOURS[level]
        self.signature_bar.setStyleSheet(
            f"#signatures {{ background: {background}; border-bottom: 1px solid {border}; }}"
            "QLabel { color: #202020; }" + _BUTTON_STYLE.format(edge="#a0a0a8"))
        self.signature_text.setText(text)
        self.signature_bar.show()

    def show_restrictions(self, restricted: List[str]) -> None:
        """Name what the author has turned off, or hide the bar if nothing."""
        if restricted:
            listed = ", ".join(restricted[:-1]) + (" and " if len(restricted) > 1 else "") \
                + restricted[-1]
            self.restriction_text.setText(
                f"<b>Restricted by the document's author:</b> {listed} "
                f"{'is' if len(restricted) == 1 else 'are'} turned off.")
        self.restriction_bar.setVisible(bool(restricted))

    def release_memory(self) -> None:
        """Out of sight: drop rendered pages and close pdfium's open pages.
        Both come back, for the pages on screen only, when it's shown again."""
        if self.view.renderer is not None:
            self.view.renderer.clear()
        if self.document is not None:
            self.document.release_pages()
