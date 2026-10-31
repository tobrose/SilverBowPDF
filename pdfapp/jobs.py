"""Print and Recognize Text: long jobs over many pages, with progress."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter
from PySide6.QtWidgets import QApplication, QDialog, QProgressDialog, QMessageBox

from .printing import (
    LOW_RES_DPI,
    PRINT_DPI,
    PrintError,
    available_printers,
    default_printer,
    print_pages,
)
from .printpreview import PrintPreview


class JobsMixin:
    """MainWindow: printing (our preview, then Windows' dialog) and OCR."""

    # ---- printing ------------------------------------------------------

    def print_dialog(self) -> None:
        doc = self.view.document
        if doc is None or not self._allowed(self.access.print, "printing it"):
            return
        self.view.commit_text_edit()  # what's being typed prints too
        self.view.commit_field_edit()
        names = available_printers()
        if not names:
            QMessageBox.warning(
                self, "Print",
                "Windows reports no printers in this session, so there's nothing to "
                "print to.\n\nOn a remote desktop, printers can take a moment to appear "
                "after you log on -- try again shortly. If none ever appear, ask IT.")
            return
        printer = QPrinter(QPrinter.HighResolution)
        printer.setDocName(doc.title)
        if not printer.isValid() or printer.printerName() not in names:
            # A session with printers but no default one (a remote desktop
            # session can be like that): start the dialog on a real printer
            # rather than leave Windows' dialog to cope with none.
            printer.setPrinterName(default_printer() or names[0])
        selected = self.sidebar.selected_rows() if self.sidebar_dock.isVisible() else []
        # Our preview first (user's choice): Windows 11's dialog can't preview
        # this app, and says so. Its page choice carries into Windows' dialog.
        preview = PrintPreview(self, self.view.renderer, printer, self.view.current_page,
                               selected)
        if preview.exec() != QDialog.Accepted:
            return
        preview.apply_to(printer)
        dialog = QPrintDialog(printer, self)
        dialog.setMinMax(1, doc.page_count)
        options = (QAbstractPrintDialog.PrintPageRange | QAbstractPrintDialog.PrintCurrentPage
                   | QAbstractPrintDialog.PrintCollateCopies)
        if len(selected) > 1:
            options |= QAbstractPrintDialog.PrintSelection  # the pages picked in the sidebar
        dialog.setOptions(options)
        if dialog.exec() != QDialog.Accepted:
            return
        self.print_to(printer, self.pages_to_print(printer, selected))

    def pages_to_print(self, printer: QPrinter, selected: list[int]) -> list[int]:
        count = self.view.document.page_count
        which = printer.printRange()
        if which == QPrinter.PageRange:
            pages = list(range(max(printer.fromPage(), 1) - 1, min(printer.toPage(), count)))
        elif which == QPrinter.CurrentPage:
            pages = [self.view.current_page]
        elif which == QPrinter.Selection and selected:
            pages = sorted(selected)
        else:
            pages = list(range(count))
        copies = printer.copyCount()
        if copies > 1 and not printer.supportsMultipleCopies():
            # The driver can't do copies itself; send them as extra sheets.
            printer.setCopyCount(1)
            pages = pages * copies if printer.collateCopies() \
                else [p for p in pages for _ in range(copies)]
        return pages

    def print_to(self, printer: QPrinter, pages: list[int]) -> None:
        progress = QProgressDialog("Printing…", "Cancel", 0, len(pages), self)
        progress.setWindowTitle("Print")
        progress.setWindowModality(Qt.WindowModal)  # the document can't change mid-job
        progress.setMinimumDuration(400)

        def tick(n: int) -> bool:
            progress.setValue(n)
            progress.setLabelText(f"Printing page {n + 1} of {len(pages)}…")
            QApplication.processEvents()
            return not progress.wasCanceled()

        # "Low-resolution printing only" is a real restriction authors set.
        dpi = PRINT_DPI if self.access.print_high else LOW_RES_DPI
        try:
            failures = print_pages(self.view.renderer, printer, pages, tick, max_dpi=dpi)
        except PrintError as exc:
            progress.close()
            QMessageBox.critical(self, "Print", str(exc))
            return
        canceled = progress.wasCanceled()
        progress.close()
        if failures:
            listed = "\n".join(f"Page {i + 1}: {err}" for i, err in failures[:10])
            QMessageBox.warning(self, "Print", "These pages could not be printed "
                                f"(their sheets say so):\n\n{listed}")
        elif canceled:
            self.statusBar().showMessage("Printing cancelled", 5000)
        else:
            where = printer.printerName() or printer.outputFileName()
            self.statusBar().showMessage(
                f"Sent {len(pages)} page{'s' if len(pages) != 1 else ''} to {where}", 8000)

    # ---- OCR -----------------------------------------------------------

    def recognize_text(self, pages: Optional[list[int]] = None) -> None:
        """Windows OCR on the scanned pages among `pages` (default: the
        sidebar selection if it's several pages, else the whole document).
        One undoable edit; saved into the file like any other."""
        doc = self.view.document
        if doc is None or self.history is None or not self._allowed(
                self.access.modify, "changing its content (OCR adds text to the pages)"):
            return
        self.view.commit_text_edit()
        self.view.commit_field_edit()
        from . import ocr  # winrt loads here, not at startup
        try:
            language = ocr.language()
        except ocr.OcrUnavailable as exc:
            QMessageBox.warning(self, "Recognize Text", str(exc))
            return
        if pages is None:
            selected = self.sidebar.selected_rows() if self.sidebar_dock.isVisible() else []
            pages = selected if len(selected) > 1 else list(range(doc.page_count))
        todo = [i for i in sorted(pages) if ocr.needs_ocr(doc, i)]
        if not todo:
            QMessageBox.information(
                self, "Recognize Text",
                "These pages already have text, so there's nothing to recognise. "
                "(OCR is for scanned pages -- ones that are just a picture.)")
            return

        progress = QProgressDialog("Recognizing text…", "Cancel", 0, len(todo), self)
        progress.setWindowTitle("Recognize Text")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)

        def tick(n: int) -> bool:
            progress.setValue(n)
            progress.setLabelText(f"Recognizing page {todo[n] + 1} ({n + 1} of {len(todo)})…")
            QApplication.processEvents()
            return not progress.wasCanceled()

        try:
            found, failures = ocr.recognize(doc, todo, tick)
        except ocr.OcrUnavailable as exc:
            progress.close()
            QMessageBox.warning(self, "Recognize Text", str(exc))
            return
        progress.close()
        empty = [doc.page_key(i) for i in todo if doc.page_key(i) in found
                 and not found[doc.page_key(i)]]
        found = {k: v for k, v in found.items() if v}  # nothing read: nothing to add
        if found:
            current = self.history.current
            state = replace(current, ocr=current.ocr.with_pages(found))
            self.history.push(state, "Recognize Text")
            self._show_state(state, self.view.current_page)
        skipped = len(pages) - len(todo)
        parts = [f"Recognised text on {len(found)} page{'s' if len(found) != 1 else ''} "
                 f"({language})."]
        if empty:
            parts.append(f"{len(empty)} page{'s' if len(empty) != 1 else ''} had no readable text.")
        if skipped:
            parts.append(f"{skipped} already had text.")
        if found:
            parts.append("Save to keep it; Ctrl+Z undoes it.")
        if failures:
            listed = "\n".join(f"Page {i + 1}: {err}" for i, err in failures[:10])
            QMessageBox.warning(self, "Recognize Text",
                                " ".join(parts) + f"\n\nThese pages failed:\n\n{listed}")
        else:
            self.statusBar().showMessage(" ".join(parts), 12000)
