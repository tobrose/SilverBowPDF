"""`ButterPDF.exe --self-test [logfile]`: prove a build actually works.

A frozen app that launches can still be missing the one DLL or data file a
real operation needs -- pdfium's version.json, qpdf's runtime, Qt's platform
plugin -- and in a machine image you find out when a user opens a PDF. This runs
the real code paths (render through pdfium into a QImage, edit, save through
pikepdf, reopen, open an encrypted file) against generated PDFs, writes a
report, and exits 0 on success, 1 on any failure. No window is shown.

Default log: %TEMP%\\ButterPDF-selftest.log
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
    log = Path(log_path) if log_path else Path(tempfile.gettempdir()) / "ButterPDF-selftest.log"
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
    lines.append(f"ButterPDF {__version__} self-test  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"python {platform.python_version()}  frozen={getattr(sys, 'frozen', False)}  "
                 f"{platform.platform()}  exe={sys.executable}")

    state: dict = {}
    work = Path(tempfile.mkdtemp(prefix="butterpdf-selftest-"))

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
            return f"password prompt path works, {doc.page_count} pages"

    step("import libraries", imports)
    if not failures:
        step("start Qt", qt)
        step("create test PDFs (pikepdf)", make_pdfs)
        step("render a page (pdfium -> QImage)", render)
        step("edit, merge, save, reopen", edit_and_save)
        step("open encrypted file", encrypted)

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
