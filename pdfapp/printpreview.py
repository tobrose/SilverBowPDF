"""Print preview: the step before Windows' print dialog (user's choice).

Windows 11's print dialog can only preview apps that hand it pages in its
own way; for everything else it says "This app doesn't support print
preview". So Print shows this first: each sheet exactly as it will print --
printing.draw_page, the same code the printer gets -- one at a time, with
the pages to print chosen here. Print… then opens Windows' dialog with
that range already set, for printer, copies and the rest.

One sheet is rendered at a time, at screen resolution, and only that one
kept: a long document doesn't fill a shared host's memory, and paging
through it repaints one small area for a remote desktop to send.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPageLayout, QPainter, QPixmap
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .printing import PRINT_DPI, draw_page
from .rendering import PageRenderer


class _Sheet(QWidget):
    """One sheet of paper, drawn as it will print."""

    def __init__(self, parent, renderer: PageRenderer, printer: QPrinter):
        super().__init__(parent)
        self.renderer = renderer
        layout = printer.pageLayout()
        self.paper = layout.fullRectPoints()           # whole sheet, points
        self.printable = layout.paintRectPoints()      # inside the margins
        self.index: Optional[int] = None
        self._cache = None                             # (key, QPixmap) of the sheet shown
        self.setMinimumSize(QSize(320, 400))
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    def show_page(self, index: Optional[int]) -> None:
        if index != self.index:
            self.index = index
            self.update()

    def _sheet_rect(self) -> QRectF:
        margin = 16
        w, h = self.width() - 2 * margin, self.height() - 2 * margin
        scale = min(w / self.paper.width(), h / self.paper.height())
        sw, sh = self.paper.width() * scale, self.paper.height() * scale
        return QRectF((self.width() - sw) / 2, (self.height() - sh) / 2, sw, sh)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#5a5a60"))
        if self.index is None:
            return
        sheet = self._sheet_rect()
        dpr = self.devicePixelRatioF()
        key = (self.index, sheet.width(), sheet.height(), dpr)
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, self._render(sheet.size().toSize(), dpr))
        painter.drawPixmap(sheet.topLeft(), self._cache[1])
        painter.setPen(QColor(0, 0, 0, 90))
        painter.drawRect(sheet.adjusted(0, 0, -1, -1))

    def _render(self, size: QSize, dpr: float) -> QPixmap:
        pixmap = QPixmap(size * dpr)
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(QColor("#ffffff"))
        scale = size.width() / self.paper.width()  # widget px per point
        area = QRectF((self.printable.left() - self.paper.left()) * scale,
                      (self.printable.top() - self.paper.top()) * scale,
                      self.printable.width() * scale, self.printable.height() * scale)
        painter = QPainter(pixmap)
        try:
            draw_page(painter, self.renderer, self.index, area, PRINT_DPI / 72.0, oversample=dpr)
        finally:
            painter.end()
        return pixmap


class PrintPreview(QDialog):
    """Sheets as they'll print, and which pages. On Print… (accept), call
    apply_to(printer) to carry the page choice into Windows' dialog."""

    def __init__(self, parent, renderer: PageRenderer, printer: QPrinter,
                 current_page: int, selected: List[int]):
        super().__init__(parent)
        self.setWindowTitle("Print Preview")
        self.resize(720, 860)
        count = renderer.document.page_count
        self.count, self.current_page = count, current_page
        self.selected = sorted(selected) if len(selected) > 1 else []
        layout = QVBoxLayout(self)

        self.sheet = _Sheet(self, renderer, printer)
        layout.addWidget(self.sheet, 1)

        nav = QHBoxLayout()
        self.prev = QPushButton("◀ Previous", self, autoDefault=False)
        self.next = QPushButton("Next ▶", self, autoDefault=False)
        self.where = QLabel(self, alignment=Qt.AlignCenter)
        self.prev.clicked.connect(lambda: self._go(-1))
        self.next.clicked.connect(lambda: self._go(+1))
        nav.addWidget(self.prev)
        nav.addWidget(self.where, 1)
        nav.addWidget(self.next)
        layout.addLayout(nav)

        pages = QHBoxLayout()
        pages.addWidget(QLabel("Print:", self))
        self.all = QRadioButton(f"All {count} pages", self, checked=True)
        self.current = QRadioButton(f"Current page ({current_page + 1})", self)
        pages.addWidget(self.all)
        pages.addWidget(self.current)
        self.selection = None
        if self.selected:
            self.selection = QRadioButton(f"Selected pages ({len(self.selected)})", self)
            pages.addWidget(self.selection)
        self.range = QRadioButton("Pages", self)
        self.first = QSpinBox(self, minimum=1, maximum=count, value=1)
        self.last = QSpinBox(self, minimum=1, maximum=count, value=count)
        pages.addWidget(self.range)
        pages.addWidget(self.first)
        pages.addWidget(QLabel("to", self))
        pages.addWidget(self.last)
        pages.addStretch(1)
        layout.addLayout(pages)
        for radio in (self.all, self.current, self.range, self.selection):
            if radio is not None:
                radio.toggled.connect(self._pages_changed)
        for spin in (self.first, self.last):
            spin.valueChanged.connect(self._range_typed)

        paper = printer.pageLayout()
        # Compare, don't test truthiness: PySide6 enums are always truthy,
        # so "if orientation()" called every sheet landscape (seen).
        landscape = paper.orientation() == QPageLayout.Landscape
        note = QLabel(f"{paper.pageSize().name()}, "
                      f"{'landscape' if landscape else 'portrait'}, on "
                      f"{printer.printerName() or 'the default printer'}. Printer, copies "
                      f"and paper can be changed in the next step.", self)
        note.setWordWrap(True)
        note.setStyleSheet("color: #808088;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        self.print_button = buttons.addButton("Print…", QDialogButtonBox.AcceptRole)
        self.print_button.setDefault(True)
        self.print_button.setFocus()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sheets: List[int] = []
        self._at = 0
        self._pages_changed()

    # -- which pages

    def pages(self) -> List[int]:
        """Displayed indices to print, in order."""
        if self.current.isChecked():
            return [self.current_page]
        if self.selection is not None and self.selection.isChecked():
            return list(self.selected)
        if self.range.isChecked():
            first, last = sorted((self.first.value(), self.last.value()))
            return list(range(first - 1, last))
        return list(range(self.count))

    def _range_typed(self) -> None:
        if self.range.isChecked():
            self._pages_changed()
        else:
            self.range.setChecked(True)  # typing a range means "these pages"

    def _pages_changed(self, *_) -> None:
        self._sheets = self.pages()
        self._at = 0
        self._show()

    # -- paging

    def _go(self, step: int) -> None:
        self._at = max(0, min(len(self._sheets) - 1, self._at + step))
        self._show()

    def _show(self) -> None:
        n = len(self._sheets)
        self.sheet.show_page(self._sheets[self._at] if n else None)
        self.where.setText(f"Sheet {self._at + 1} of {n}  ·  page {self._sheets[self._at] + 1}"
                           if n else "No pages")
        self.prev.setEnabled(self._at > 0)
        self.next.setEnabled(self._at < n - 1)
        self.print_button.setEnabled(n > 0)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_PageDown, Qt.Key_Right, Qt.Key_Down):
            self._go(+1)
        elif event.key() in (Qt.Key_PageUp, Qt.Key_Left, Qt.Key_Up):
            self._go(-1)
        else:
            super().keyPressEvent(event)

    def apply_to(self, printer: QPrinter) -> None:
        """Carry the page choice into the printer, so Windows' dialog opens
        with it -- and MainWindow.pages_to_print reads it back after."""
        if self.current.isChecked():
            printer.setPrintRange(QPrinter.CurrentPage)
        elif self.selection is not None and self.selection.isChecked():
            printer.setPrintRange(QPrinter.Selection)
        elif self.range.isChecked():
            first, last = sorted((self.first.value(), self.last.value()))
            printer.setPrintRange(QPrinter.PageRange)
            printer.setFromTo(first, last)
        else:
            printer.setPrintRange(QPrinter.AllPages)
