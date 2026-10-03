"""Printing: the page as shown minus the form-field tint, fitted to the
sheet, turned when that's bigger; the dialog's page ranges.

Prints to PDF files (QPrinter's PDF output), never to a real printer. UI
parts run on the offscreen platform with INI settings -- never the real
registry.

Run with pytest, or directly:  python tests/test_print.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtPrintSupport import QPrinter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.printing import place_on_sheet, print_pages  # noqa: E402
from pdfapp.rendering import PageRenderer  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402
from patching import patched  # noqa: E402
from test_annots import _make_annotated_pdf  # noqa: E402  (3 pages: annots, a form field, /Rotate 90)

_app = QApplication.instance() or QApplication([])


def _pdf_printer(path) -> QPrinter:
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(str(path))
    return printer


def _rgb(image, x, y):
    c = image.pixelColor(x, y)
    return c.red(), c.green(), c.blue()


def test_place_on_sheet():
    turned, s, target = place_on_sheet(612, 792, 2400, 3200)  # portrait on portrait
    assert not turned and abs(s - min(2400 / 612, 3200 / 792)) < 1e-9
    assert abs(target.center().x() - 1200) < 1e-6 and abs(target.center().y() - 1600) < 1e-6
    turned, s, target = place_on_sheet(792, 612, 2400, 3200)  # landscape on portrait
    assert turned, "a landscape page turns to fill portrait paper"
    assert target.height() > target.width(), "target is in sheet orientation"
    assert abs(target.width() - 612 * s) < 1e-6
    turned, _, _ = place_on_sheet(600, 610, 2400, 2420)
    assert not turned, "no turning for a near-square page"


def test_print_render_drops_the_field_tint_keeps_the_marks():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            r = PageRenderer(doc, max_pages=0)
            # Page 2: a text field at x 100-400, y 500-530 (PDF space); at
            # scale 1 on an unrotated 612x792 page, image y = 792 - pdf y.
            screen, paper = r.image(1, 1.0), r.print_image(1, 1.0)
            in_field, beside = (390, 792 - 515), (390, 792 - 560)
            assert _rgb(screen, *in_field) != _rgb(screen, *beside), "screen tints fields"
            a, b = _rgb(paper, *in_field), _rgb(paper, *beside)
            assert max(abs(i - j) for i, j in zip(a, b)) <= 3, f"no tint on paper: {a} vs {b}"
            # max_pages=0 caches nothing, so this is a fresh screen render.
            assert _rgb(r.image(1, 1.0), *in_field) == _rgb(screen, *in_field), \
                "the screen's tint is back after printing"

            # Page 1: a red-stroked square at x 100-250, y 300-400 prints.
            red = _rgb(r.print_image(0, 1.0), 102, 792 - 350)
            assert red[0] > 200 and red[1] < 80 and red[2] < 80, red


def test_print_to_pdf_file():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "printed.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            ticks = []
            failures = print_pages(PageRenderer(doc, max_pages=0), _pdf_printer(out), [0, 1, 2],
                                   lambda n: ticks.append(n) or True)
        assert failures == [] and ticks == [0, 1, 2]
        with PdfDocument(out) as printed:
            assert printed.page_count == 3
            sizes = {tuple(round(v) for v in printed.transform(i).display_size_pt)
                     for i in range(3)}
            assert len(sizes) == 1, f"one paper size for every sheet, turned page included: {sizes}"
            # Page 3 is portrait content shown sideways by /Rotate 90. Turned
            # back onto portrait paper it must read upright: its title, near
            # the top of the content, lands in the top half of the sheet.
            text = printed.find_text(2, "Annotated")
            assert not text, "the sheet is an image; find it by ink instead"
            sheet = PageRenderer(printed, max_pages=0).image(2, 0.5)
            ink_rows = [y for y in range(sheet.height())
                        if any(sheet.pixelColor(x, y).lightness() < 60
                               for x in range(sheet.width() // 8, sheet.width() * 7 // 8, 3))]
            # The title is the largest dark text; it sits near the top when upright.
            assert ink_rows and min(ink_rows) < sheet.height() * 0.25, \
                f"upright: dark ink starts at row {min(ink_rows)} of {sheet.height()}"


def test_cancel_stops_the_job():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "printed.pdf")
        _make_annotated_pdf(src)
        printer = _pdf_printer(out)
        asked = []
        with PdfDocument(src) as doc:
            print_pages(PageRenderer(doc, max_pages=0), printer, [0, 1, 2],
                        lambda n: asked.append(n) or n < 1)  # cancel before the second sheet
        assert asked == [0, 1], "nothing rendered after the cancel"
        # A real Windows printer aborts the job (the driver drops it); Qt's
        # PDF output can't abort, so it closes with what was already drawn.
        if out.exists() and out.stat().st_size:
            with PdfDocument(out) as printed:
                assert printed.page_count == 1, "no sheet after the cancel"


def test_dialog_page_ranges():
    from pdfapp.mainwindow import MainWindow
    with tempfile.TemporaryDirectory() as tmp:
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        try:
            w.view.set_document(PdfDocument(src))
            printer = _pdf_printer(Path(tmp, "x.pdf"))
            printer.setPrintRange(QPrinter.AllPages)
            assert w.pages_to_print(printer, []) == [0, 1, 2]
            printer.setPrintRange(QPrinter.PageRange)
            printer.setFromTo(2, 3)
            assert w.pages_to_print(printer, []) == [1, 2]
            printer.setPrintRange(QPrinter.CurrentPage)
            assert w.pages_to_print(printer, []) == [w.view.current_page]
            printer.setPrintRange(QPrinter.Selection)
            assert w.pages_to_print(printer, [2, 0]) == [0, 2], "sidebar selection, in order"

            printer.setPrintRange(QPrinter.AllPages)
            printer.setCopyCount(2)
            printer.setCollateCopies(True)
            pages = w.pages_to_print(printer, [])
            if printer.supportsMultipleCopies():
                assert pages == [0, 1, 2], "the driver makes the copies"
            else:
                assert pages == [0, 1, 2, 0, 1, 2], "collated copies as extra sheets"
            assert w.act_print.shortcut().toString() == "Ctrl+P"
        finally:
            w.view.set_document(None)
            w.close()


def _Patched(**names):
    """Stand-ins for one test: Windows' print dialog would block, and this
    machine's printers aren't the users' (patching.patched)."""
    return patched(**names)


def _fake_dialog(accept: bool, seen: list):
    """Stands in for Windows' print dialog: records the printer it was
    given, then OK or Cancel."""
    from PySide6.QtWidgets import QDialog

    class Fake:
        def __init__(self, printer, parent):
            self.printer = printer

        def setMinMax(self, *a): pass
        def setOptions(self, *a): pass

        def exec(self):
            seen.append((self.printer.printerName(), self.printer.docName()))
            return QDialog.Accepted if accept else QDialog.Rejected
    return Fake


def _fake_preview(accept: bool, shown: list):
    """Stands in for the print preview: OK (all pages) or Cancel."""
    from PySide6.QtWidgets import QDialog

    class Fake:
        def __init__(self, parent, renderer, printer, current, selected):
            shown.append(printer.printerName())

        def exec(self):
            return QDialog.Accepted if accept else QDialog.Rejected

        def apply_to(self, printer):
            printer.setPrintRange(QPrinter.AllPages)
    return Fake


def test_print_preview():
    from pdfapp.printpreview import PrintPreview
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)  # page 3 is /Rotate 90: shown landscape
        with PdfDocument(src) as doc:
            printer = _pdf_printer(Path(tmp, "x.pdf"))
            preview = PrintPreview(None, PageRenderer(doc, max_pages=0), printer, 1, [0, 2])
            preview.resize(600, 800)
            from PySide6.QtWidgets import QLabel
            notes = " ".join(l.text() for l in preview.findChildren(QLabel))
            assert "portrait" in notes and "landscape" not in notes, notes
            assert preview.pages() == [0, 1, 2] and "Sheet 1 of 3" in preview.where.text()
            preview._go(+1)
            assert "page 2" in preview.where.text() and preview.prev.isEnabled()

            preview.current.setChecked(True)
            assert preview.pages() == [1] and not preview.next.isEnabled()
            preview.selection.setChecked(True)
            assert preview.pages() == [0, 2], "the sidebar selection"
            preview.first.setValue(2)  # typing a range selects "Pages"
            assert preview.range.isChecked() and preview.pages() == [1, 2]
            preview.apply_to(printer)
            assert printer.printRange() == QPrinter.PageRange
            assert (printer.fromPage(), printer.toPage()) == (2, 3)

            # What it shows is what prints: the turned page 3, upright on
            # portrait paper, the red square on page 1, no field tint.
            preview.all.setChecked(True)
            sheets = []
            for _ in range(3):
                sheets.append(preview.sheet.grab().toImage())
                preview._go(+1)
            sheet = sheets[0]
            reds = sum(1 for x in range(0, sheet.width(), 3) for y in range(0, sheet.height(), 3)
                       if sheet.pixelColor(x, y).red() > 200 and sheet.pixelColor(x, y).green() < 80)
            assert reds > 5, "page 1's red square is on the sheet"
            assert all(s.width() == sheets[0].width() for s in sheets), "one paper size"
            preview.deleteLater()


def test_print_menu_end_to_end():
    """File > Print as the user runs it -- the path that crashed in 0.6.0
    (doc.title called as a method) while every lower-level test passed."""
    from pdfapp.mainwindow import MainWindow
    with tempfile.TemporaryDirectory() as tmp:
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        warnings, printed, seen = [], [], []
        w.print_to = lambda printer, pages: printed.append((printer.printerName(), pages))

        class Box:
            @staticmethod
            def warning(parent, title, text, *a):
                warnings.append(text)
        from pdfapp.printing import available_printers
        real = available_printers()  # Qt only selects printers that exist
        assert real, "this test needs at least one printer installed (any, even PDF)"

        class NoDefault(QPrinter):
            """A session with no default printer: unset until one is chosen."""
            def __init__(self, *a):
                super().__init__(*a)
                self.chosen = False

            def isValid(self):
                return self.chosen

            def setPrinterName(self, name):
                self.chosen = True
                super().setPrinterName(name)
        previewed = []
        try:
            w.view.set_document(PdfDocument(src))
            # Preview, then OK in Windows' dialog: it prints, the job named
            # after the file.
            with _Patched(QPrintDialog=_fake_dialog(True, seen),
                          PrintPreview=_fake_preview(True, previewed)):
                w.act_print.trigger()  # the real menu action: a click, or Ctrl+P
            assert previewed, "the preview comes first"
            assert printed and printed[0][1] == [0, 1, 2], printed
            assert seen[-1][1] == "a.pdf", seen

            # No default printer: preview and dialog still start on a real one.
            printed.clear()
            with _Patched(QPrinter=NoDefault, default_printer=lambda: "",
                          QPrintDialog=_fake_dialog(True, seen),
                          PrintPreview=_fake_preview(True, previewed)):
                w.print_dialog()
            assert previewed[-1] == real[0] and seen[-1][0] == real[0] and printed

            # Cancel in the preview: Windows' dialog never opens.
            printed.clear()
            seen.clear()
            with _Patched(QPrintDialog=_fake_dialog(True, seen),
                          PrintPreview=_fake_preview(False, previewed)):
                w.print_dialog()
            assert not seen and not printed

            # Cancel in Windows' dialog: nothing more.
            with _Patched(QPrintDialog=_fake_dialog(False, seen),
                          PrintPreview=_fake_preview(True, previewed)):
                w.print_dialog()
            assert not printed

            # No printers in the session: say so, don't just do nothing.
            with _Patched(available_printers=lambda: [], QMessageBox=Box):
                w.print_dialog()
            assert warnings and "no printers" in warnings[0] and not printed
        finally:
            w.view.set_document(None)
            w.close()


def test_errors_in_actions_are_shown_not_lost():
    import pdfapp.errors as errors
    shown = []

    class Box:
        @staticmethod
        def critical(parent, title, text):
            shown.append(text)
    import PySide6.QtWidgets as widgets
    saved, widgets.QMessageBox = widgets.QMessageBox, Box
    saved_log = errors.LOG
    assert "AppData" in str(saved_log) and "Temp" not in str(saved_log), \
        "roaming AppData survives logoff on a non-persistent desktop; %TEMP% doesn't"
    with tempfile.TemporaryDirectory() as tmp:
        errors.LOG = Path(tmp, "SilverbowPDF", "errors.log")  # folder made on first use
        try:
            try:
                "".title()()  # the very shape of 0.6.0's print bug
            except TypeError:
                errors.report(*sys.exc_info())
            assert shown and "TypeError" in shown[0] and str(errors.LOG) in shown[0]
            assert "Traceback" in errors.LOG.read_text(encoding="utf-8")
            # It roams with the profile, so it stays small: the oldest goes.
            errors.LOG.write_text("--- old\nTraceback x\n" * 40000, encoding="utf-8")
            try:
                raise ValueError("newest")
            except ValueError:
                errors.report(*sys.exc_info())
            text = errors.LOG.read_text(encoding="utf-8")
            assert len(text.encode()) < errors.LOG_LIMIT and "ValueError: newest" in text
            assert text.startswith("(older entries dropped)\n--- old\n")
        finally:
            widgets.QMessageBox = saved
            errors.LOG = saved_log


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
