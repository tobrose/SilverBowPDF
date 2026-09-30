"""Ctrl+F: the find bar above the page view.

Matches come from pdfium's own search (PdfDocument.find_text), page by page.
Pages are scanned in short slices on a timer, starting at the current page
and wrapping, so a long document never freezes the window and the first
match found is the next one after where the user is reading. Next/Previous
scan any page they need that the background pass hasn't reached yet.

A phrase broken across two lines isn't found -- pdfium's text has a line
break there, not a space.
"""

from __future__ import annotations

import time
from typing import Dict, List

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QWidget,
)

from .pageview import PdfView

SLICE_SECONDS = 0.03   # per timer tick; the window stays responsive
TYPING_DELAY_MS = 250  # wait for a pause in typing before searching


class FindBar(QWidget):
    closed = Signal()

    def __init__(self, view: PdfView, parent=None):
        super().__init__(parent)
        self.view = view
        self._doc = None
        self._query = None             # (text, match case, whole words)
        self._by_page: Dict[int, list] = {}  # scanned pages -> [(start, end, rects)]
        self._queue: List[int] = []    # pages still to scan, in scan order
        self._failed: List[int] = []   # pages pdfium couldn't search
        self._current = None           # (page, start) of the current match
        self._want_first = False       # select the first match the scan finds

        self.edit = QLineEdit(self, placeholderText="Find in document")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(260)
        self.edit.installEventFilter(self)
        self.prev_button = QPushButton("Previous", self)
        self.next_button = QPushButton("Next", self)
        self.case_box = QCheckBox("Match case", self)
        self.word_box = QCheckBox("Whole words", self)
        self.status = QLabel("", self)
        close = QToolButton(self, text="✕", autoRaise=True, toolTip="Close (Esc)")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(QLabel("Find:", self))
        layout.addWidget(self.edit)
        layout.addWidget(self.prev_button)
        layout.addWidget(self.next_button)
        layout.addSpacing(8)
        layout.addWidget(self.case_box)
        layout.addWidget(self.word_box)
        layout.addSpacing(8)
        layout.addWidget(self.status, 1)
        layout.addWidget(close)

        self._typing = QTimer(self, singleShot=True, interval=TYPING_DELAY_MS,
                              timeout=self._restart)
        self._scanner = QTimer(self, interval=0, timeout=self._scan_slice)
        self.edit.textEdited.connect(lambda _: self._typing.start())
        self.edit.returnPressed.connect(self.find_next)
        self.prev_button.clicked.connect(self.find_previous)
        self.next_button.clicked.connect(self.find_next)
        self.case_box.toggled.connect(lambda _: self._restart())
        self.word_box.toggled.connect(lambda _: self._restart())
        close.clicked.connect(self.close_bar)
        # Indices change when pages are edited or another file is opened;
        # rescan quietly -- highlights only, no jumping the view around.
        view.pages_rebuilt.connect(lambda: QTimer.singleShot(0, self._rescan))
        view.text_layer_changed.connect(lambda: QTimer.singleShot(0, self._rescan))
        self.hide()

    # ---- opening and closing -------------------------------------------

    def open(self) -> None:
        selected = self.view.selected_text().strip()
        if selected and "\n" not in selected and "\r" not in selected and len(selected) <= 200:
            self.edit.setText(selected)
            self._query = None  # a new query even if it was open already
        self.show()
        self.edit.setFocus(Qt.ShortcutFocusReason)
        self.edit.selectAll()
        if self._query != self._wanted():
            self._restart()

    def close_bar(self) -> None:
        self._stop()
        self._query = None
        self._current = None
        self.view.clear_search()
        self.status.setText("")
        self.hide()
        self.view.setFocus(Qt.OtherFocusReason)
        self.closed.emit()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.close_bar()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                    and event.modifiers() & Qt.ShiftModifier:
                self.find_previous()
                return True
        return super().eventFilter(obj, event)

    # ---- searching -----------------------------------------------------

    def _wanted(self):
        return (self.edit.text(), self.case_box.isChecked(), self.word_box.isChecked())

    def _stop(self) -> None:
        self._typing.stop()
        self._scanner.stop()
        self._queue = []

    def _restart(self, jump: bool = True) -> None:
        """Start over with what's in the box, scanning from the current page."""
        self._stop()
        self.view.clear_search()
        self._doc = self.view.document
        self._query = self._wanted()
        self._by_page = {}
        self._failed = []
        self._current = None
        self._want_first = jump
        if self._doc is None or not self._query[0]:
            self.status.setText("" if self._doc is not None else "No document open")
            return
        count, start = self._doc.page_count, self.view.current_page
        self._queue = [(start + i) % count for i in range(count)]
        self._scanner.start()
        self._show_status()

    def _rescan(self) -> None:
        if self.isVisible() and self.edit.text():
            self._restart(jump=False)

    def _page_hits(self, page: int) -> list:
        """Matches on a page, scanning it now if the background pass hasn't."""
        hits = self._by_page.get(page)
        if hits is None:
            text, case, words = self._query
            try:
                hits = self._doc.find_text(page, text, case, words)
            except Exception as exc:  # one bad page mustn't stop the search -- but say so
                hits = []
                self._failed.append(page)
                self.view.message.emit(f"Page {page + 1} could not be searched: {exc}")
            self._by_page[page] = hits
            self.view.set_search_hits(page, hits)
        return hits

    def _scan_slice(self) -> None:
        if self.view.document is not self._doc:
            self._stop()
            return
        deadline = time.perf_counter() + SLICE_SECONDS
        while self._queue and time.perf_counter() < deadline:
            page = self._queue.pop(0)
            if page in self._by_page:
                continue  # Next/Previous got there first
            hits = self._page_hits(page)
            if self._want_first and hits:
                # Scan order is reading order from the current page, so the
                # first page with a match holds the next match.
                self._want_first = False
                self._select(page, hits[0])
        if not self._queue:
            self._scanner.stop()
            self._want_first = False
        self._show_status()

    def _ready(self) -> bool:
        """Is there a live query to step through? Starts one if the box
        changed (Enter straight after typing shouldn't wait for the pause)."""
        if not self.isVisible():
            self.open()
        if self._query != self._wanted() or self.view.document is not self._doc:
            self._restart()
            return False  # the scan selects the first match itself
        return self._doc is not None and bool(self._query[0])

    def find_next(self) -> None:
        if self._ready():
            self._step(+1)

    def find_previous(self) -> None:
        if self._ready():
            self._step(-1)

    def _step(self, direction: int) -> None:
        self._want_first = False
        count = self._doc.page_count
        if self._current is None:
            page, after = self.view.current_page, None
        else:
            page, after = self._current
        # Visit every page once in `direction`, then the starting page again
        # for the matches on the other side of the current one.
        for step in range(count + 1):
            p = (page + direction * step) % count
            hits = self._page_hits(p)
            if direction < 0:
                hits = hits[::-1]
            for hit in hits:
                if step == 0 and after is not None and \
                        (hit[0] <= after if direction > 0 else hit[0] >= after):
                    continue
                self._select(p, hit)
                self._show_status()
                return
        self._show_status()

    def _select(self, page: int, hit) -> None:
        self._current = (page, hit[0])
        self.view.set_search_current(self._current)
        self.view.reveal(page, hit[2])

    def _show_status(self) -> None:
        total = sum(len(h) for h in self._by_page.values())
        busy = "…" if self._queue else ""
        if total == 0:
            text = "Searching…" if self._queue else "No matches"
        elif self._current is None:
            text = f"{total} matches{busy}"
        else:
            page, start = self._current
            before = sum(len(h) for p, h in self._by_page.items() if p < page)
            within = next((i for i, h in enumerate(self._by_page.get(page, ())) if h[0] == start), 0)
            text = f"{before + within + 1} of {total}{busy}"
        if self._failed:
            pages = ", ".join(str(p + 1) for p in sorted(self._failed)[:5])
            text += f"  (couldn't search page {pages})"
        self.status.setText(text)
