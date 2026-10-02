"""`SilverbowPDF.exe --self-test [logfile]`: prove a build actually works.

A frozen app that launches can still be missing the one DLL or data file a
real operation needs -- pdfium's version.json, qpdf's runtime, Qt's platform
plugin -- and in a machine image you find out when a user opens a PDF. This runs
the real code paths (render through pdfium into a QImage, edit, save through
pikepdf, reopen, open an encrypted file) against generated PDFs, writes a
report, and exits 0 on success, 1 on any failure. No window is shown.

Default log: %TEMP%\\SilverbowPDF-selftest.log
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable, List, Optional


def run(log_path: Optional[str] = None) -> int:
    log = Path(log_path) if log_path else Path(tempfile.gettempdir()) / "SilverbowPDF-selftest.log"
    lines: List[str] = []
    failures = 0

    def step(name: str, fn: Callable[[], str]) -> None:
        nonlocal failures
        started = time.perf_counter()
        try:
            detail = fn()
            lines.append(f"PASS  {name} ({(time.perf_counter() - started) * 1000:.0f} ms) {detail}")
        except Exception:
            failures += 1
            lines.append(f"FAIL  {name}\n" + "".join(
                "      " + l for l in traceback.format_exc().splitlines(True)))

    from pdfapp import __version__
    lines.append(f"SilverbowPDF {__version__} self-test  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"python {platform.python_version()}  frozen={getattr(sys, 'frozen', False)}  "
                 f"{platform.platform()}  exe={sys.executable}")

    state: dict = {}
    work = Path(tempfile.mkdtemp(prefix="silverbowpdf-selftest-"))

    def imports() -> str:
        import pikepdf
        import pypdfium2
        import PySide6
        state["pikepdf"] = pikepdf
        return (f"PySide6 {PySide6.__version__}, pikepdf {pikepdf.__version__}, "
                f"pypdfium2 {getattr(pypdfium2, 'PYPDFIUM_INFO', '?')}")

    def qt() -> str:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([sys.argv[0]])
        state["app"] = app
        return f"platform plugin: {app.platformName()}"

    def make_pdfs() -> str:
        pikepdf = state["pikepdf"]
        for name, encryption in (("plain.pdf", None),
                                 ("locked.pdf", pikepdf.Encryption(user="pw", owner="own"))):
            pdf = pikepdf.Pdf.new()
            font = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                BaseFont=pikepdf.Name.Helvetica))
            for i, rot in enumerate((0, 90, 0)):
                pdf.add_blank_page(page_size=(612, 792))
                page = pdf.pages[-1]
                page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
                page.Contents = pdf.make_stream(
                    f"1 0 0 rg 50 50 200 100 re f BT /F1 48 Tf 72 600 Td (Page {i + 1}) Tj ET"
                    .encode())
                if rot:
                    page.Rotate = rot
            pdf.save(work / name, encryption=encryption)
        return str(work)

    def render() -> str:
        from .document import PdfDocument
        from .rendering import PageRenderer
        with PdfDocument(work / "plain.pdf") as doc:
            image = PageRenderer(doc, max_pages=0).image(1, 1.5)
            if image.isNull():
                raise RuntimeError("rendered image is null")
            # The red box must have been drawn: pdfium really rendered content.
            reds = sum(1 for x in range(0, image.width(), 7) for y in range(0, image.height(), 7)
                       if image.pixelColor(x, y).red() > 200 and image.pixelColor(x, y).green() < 60)
            if reds == 0:
                raise RuntimeError("page rendered blank")
            return f"{image.width()}x{image.height()}, content visible"

    def printing() -> str:
        # Prints to a PDF file (no paper), which runs the whole path: Qt's
        # print support bundled, pdfium's print render, the sheet layout.
        from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
        from .document import PdfDocument
        from .printing import print_pages
        from .rendering import PageRenderer
        out = work / "printed.pdf"
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(str(out))
        with PdfDocument(work / "plain.pdf") as doc:
            failures = print_pages(PageRenderer(doc, max_pages=0), printer, [0, 1])
        if failures:
            raise RuntimeError(f"pages failed to print: {failures}")
        with PdfDocument(out) as printed:
            if printed.page_count != 2:
                raise RuntimeError(f"printed {printed.page_count} sheets, expected 2")
        names = [p.printerName() for p in QPrinterInfo.availablePrinters()]
        default = QPrinterInfo.defaultPrinterName() or "none"
        return f"2 sheets to PDF; {len(names)} printer(s) visible, default: {default}"

    def recognize() -> str:
        # Windows' OCR through the bundled winrt bridge, on a rendered page.
        # Fails if the bridge didn't bundle -- or if this Windows has no OCR
        # language, which in a machine image is worth knowing before users do.
        from . import ocr
        from .document import PdfDocument
        with PdfDocument(work / "plain.pdf") as doc:
            words = ocr.recognize_page(doc, 0)
        text = "".join(w.text for w in words)
        if "Page 1" not in text:
            raise RuntimeError(f"expected 'Page 1', read {text!r}")
        return f"read {text!r} ({ocr.language()})"

    def edit_and_save() -> str:
        from .document import PdfDocument
        from .writer import commit, write_pdf
        out = work / "out.pdf"
        with PdfDocument(work / "plain.pdf") as doc:
            f = doc.add_source(work / "plain.pdf")
            doc.pages = doc.pages.rotated([1], 270).deleted([2]).inserted(doc.source_refs(f)[:1], 0)
            expected = [doc.page_rotation(i) for i in range(doc.page_count)]
            commit(write_pdf(doc.sources, doc.pages, out), out)
        with PdfDocument(out) as saved:
            got = [saved.page_rotation(i) for i in range(saved.page_count)]
        if got != expected:
            raise RuntimeError(f"saved rotations {got}, expected {expected}")
        return f"{len(got)} pages, rotations {got}"

    def encrypted() -> str:
        from .document import PasswordRequired, PdfDocument
        try:
            PdfDocument(work / "locked.pdf").close()
            raise RuntimeError("encrypted file opened without a password")
        except PasswordRequired:
            pass
        with PdfDocument(work / "locked.pdf", password="pw") as doc:
            count = doc.page_count
            # Save keeps the protection (qpdf copies it), and a new password
            # replaces it with AES-256: the encryption milestone, end to end.
            from .writer import commit, write_pdf
            kept, changed = work / "kept.pdf", work / "changed.pdf"
            commit(write_pdf(doc.sources, doc.pages.deleted([0]), kept), kept)
            commit(write_pdf(doc.sources, doc.pages, changed, protection="new pw"), changed)
        for path, password in ((kept, "pw"), (changed, "new pw")):
            try:
                PdfDocument(path).close()
                raise RuntimeError(f"{path.name} saved without its password")
            except PasswordRequired:
                pass
            with PdfDocument(path, password=password) as doc:
                if not doc.is_encrypted:
                    raise RuntimeError(f"{path.name} isn't encrypted")
        return f"{count} pages; saving keeps the password; a new one replaces it"

    def signatures() -> str:
        # pyHanko, cryptography and Windows' CNG/certificate store, frozen:
        # make a .pfx ID, sign, sign again through Windows' own key API,
        # check both. Also says how many signing certificates this user's
        # Windows offers -- worth knowing where it's deployed.
        import datetime as dt
        from . import digitalid, pdfsign, wincert
        pfx = work / "id.pfx"
        digitalid.create_pfx_id(pfx, "Self Test", "pw")
        look = pdfsign.Look("Self Test", dt.datetime.now(), reason=pdfsign.REASONS[1])
        once, twice = work / "signed.pdf", work / "signed2.pdf"
        pdfsign.sign(work / "plain.pdf", None, once, 0, (300, 100, 520, 170), 0, look,
                     digitalid.pfx_signer(pfx, "pw"))
        signer = wincert.imported_pfx_signer(pfx.read_bytes(), "pw")
        pdfsign.sign(once, None, twice, 1, (300, 100, 520, 170), 90, look, signer)
        mine = ([digitalid.pfx_signer(pfx, "pw").signing_cert], [])
        levels = [i.level for i in pdfsign.check(twice, trust=mine)]
        if levels != [pdfsign.OK, pdfsign.OK]:
            raise RuntimeError(f"signatures checked {levels}, expected both ok")
        windows = [i.level for i in pdfsign.check(twice)]  # Windows' roots don't know it
        if windows != [pdfsign.UNTRUSTED] * 2:
            raise RuntimeError(f"against Windows' roots: {windows}, expected untrusted")
        roots, _ = wincert.trust_certificates()
        return (f"signed twice (file ID, Windows CNG), both valid; {len(roots)} trusted "
                f"roots; {len(wincert.list_ids())} signing certificate(s) for this user")

    def icon() -> str:
        from PySide6.QtGui import QIcon
        from pdfapp import ICON_FILE
        sizes = QIcon(ICON_FILE).availableSizes()
        if not sizes:
            raise RuntimeError(f"window icon missing or unreadable: {ICON_FILE}")
        return f"{sizes[0].width()}px"

    step("import libraries", imports)
    if not failures:
        step("start Qt", qt)
        step("window icon", icon)
        step("create test PDFs (pikepdf)", make_pdfs)
        step("render a page (pdfium -> QImage)", render)
        step("print (to a PDF file)", printing)
        step("OCR (Windows)", recognize)
        step("edit, merge, save, reopen", edit_and_save)
        step("open, save and re-protect an encrypted file", encrypted)
        step("digital signatures (pyHanko, Windows CNG)", signatures)

    lines.append("")
    lines.append("RESULT: " + ("PASS" if failures == 0 else f"FAIL ({failures} step(s))"))
    report = "\n".join(lines) + "\n"
    try:
        log.write_text(report, encoding="utf-8")
    except OSError:
        pass
    if sys.stdout is not None:  # None in the windowed (console-less) build
        print(report, end="")

    try:
        for p in work.iterdir():
            p.unlink()
        work.rmdir()
    except OSError:
        pass
    return 0 if failures == 0 else 1
