"""OCR: Windows' engine reads scanned pages; the recognised words become an
invisible text layer that search and selection use before saving, and that
saving writes into the page at the same places.

Needs Windows 10/11 with an OCR language (any English install has one).
Where the engine is missing the recognition tests say SKIP -- loudly --
rather than pass. The text-layer tests run everywhere.

Run with pytest, or directly:  python tests/test_ocr.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # the UI test

import pikepdf  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402
from pikepdf import Dictionary, Name  # noqa: E402

from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.ocrlayer import OcrEdits, OcrWord, text_layer_pdf, word_matrix  # noqa: E402
from pdfapp.writer import commit, write_pdf  # noqa: E402

LINES = [(14, 700, "The quick brown fox jumps over the lazy dog."),
         (20, 600, "Invoice number 4471"),
         (12, 500, "Please remit payment within thirty days.")]


class Skip(Exception):
    pass


def _ocr():
    from pdfapp import ocr
    try:
        ocr.engine()
    except ocr.OcrUnavailable as exc:
        raise Skip(str(exc))
    return ocr


def _text_pdf(path):
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0]
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    page.Contents = pdf.make_stream(" ".join(
        f"BT /F1 {size} Tf 72 {y} Td ({text}) Tj ET" for size, y, text in LINES).encode())
    pdf.save(path)


def _make_scan(path, sideways: bool = False, dpi: int = 200):
    """An image-only page that looks like the text PDF -- a scan. sideways:
    stored turned a quarter (landscape MediaBox) with /Rotate 90 to show it
    upright, as many scanners write portrait pages."""
    text = Path(str(path) + ".text.pdf")
    _text_pdf(text)
    src = pdfium.PdfDocument(str(text))
    bitmap = src[0].render(scale=dpi / 72, rotation=270 if sideways else 0, rev_byteorder=True)
    w, h, stride = bitmap.width, bitmap.height, bitmap.stride
    assert bitmap.mode == "RGB", bitmap.mode
    raw = bytes(bitmap.buffer)
    rgb = b"".join(raw[y * stride:y * stride + w * 3] for y in range(h))
    bitmap.close()
    src.close()

    pdf = pikepdf.Pdf.new()
    size = (792, 612) if sideways else (612, 792)
    pdf.add_blank_page(page_size=size)
    page = pdf.pages[0]
    image = pdf.make_stream(zlib.compress(rgb))
    image.Type, image.Subtype = Name.XObject, Name.Image
    image.Width, image.Height, image.BitsPerComponent = w, h, 8
    image.ColorSpace, image.Filter = Name.DeviceRGB, Name.FlateDecode
    page.Resources = Dictionary(XObject=Dictionary(Im0=image))
    page.Contents = pdf.make_stream(f"q {size[0]} 0 0 {size[1]} 0 0 cm /Im0 Do Q".encode())
    if sideways:
        page.Rotate = 90
    pdf.save(path)


def _render(doc, index):
    bitmap = doc.render_bitmap(index, 0.5)
    try:
        return bytes(bitmap.buffer)
    finally:
        bitmap.close()


def _find(doc, needle):
    hits = doc.find_text(0, needle)
    assert hits, f"{needle!r} not found; page text: {doc.text_in(0, 0, max(doc.char_count(0) - 1, 0))!r}"
    return hits[0][2]


def test_text_layer_pdf_puts_words_where_asked():
    # Two words on one line, each given its box in PDF space.
    m1 = word_matrix((100, 700), (160, 700), (100, 720), len("Hello "))
    m2 = word_matrix((160, 700), (210, 700), (160, 720), len("world"))
    data = text_layer_pdf([OcrWord("Hello ", m1), OcrWord("world", m2)],
                          (0, 0, 612, 792), (0, 0, 612, 792), 0)
    doc = pdfium.PdfDocument(data)
    try:
        textpage = doc[0].get_textpage()
        assert textpage.get_text_range() == "Hello world"
        # Loose boxes, as the app uses (document._text): the font's advance
        # and ascent, not whatever a substitute font's glyph outline covers.
        left, bottom, right, top = textpage.get_charbox(0, loose=True)
        assert abs(left - 100) < 1 and abs(bottom - 700) < 1.5 and abs(top - 720) < 1.5, \
            (left, bottom, right, top)
        last = textpage.get_charbox(textpage.count_chars() - 1, loose=True)
        assert abs(last[2] - 210) < 1, last
        textpage.close()
    finally:
        doc.close()


def test_needs_ocr_only_on_pages_without_text():
    ocr = _ocr()
    with tempfile.TemporaryDirectory() as tmp:
        text, scan = Path(tmp, "t.pdf"), Path(tmp, "s.pdf")
        _text_pdf(text)
        _make_scan(scan)
        with PdfDocument(text) as doc:
            assert not ocr.needs_ocr(doc, 0)
        with PdfDocument(scan) as doc:
            assert ocr.needs_ocr(doc, 0)


def _check_scan(sideways: bool):
    ocr = _ocr()
    with tempfile.TemporaryDirectory() as tmp:
        scan, saved = Path(tmp, "scan.pdf"), Path(tmp, "saved.pdf")
        _make_scan(scan, sideways=sideways)
        with PdfDocument(scan) as doc:
            before = _render(doc, 0)
            words = ocr.recognize_page(doc, 0)
            text = "".join(w.text for w in words)
            for _, _, line in LINES:
                assert line.replace(".", "") in text.replace(".", ""), f"read {text!r}"
            doc.ocr = OcrEdits({doc.page_key(0): words})
            preview = _find(doc, "quick brown")
            assert _render(doc, 0) == before, "the text layer is invisible"

            left, bottom, right, top = preview[0]
            if not sideways:  # ink is where the text PDF drew it: x 72.., baseline 700, 14pt
                assert 60 < left < 120 and 690 < bottom < 705 and 705 < top < 725, preview
            tmp_out = write_pdf(doc.sources, doc.pages, saved, ocr=doc.ocr)
        commit(tmp_out, saved)
        with PdfDocument(saved) as doc:
            assert not ocr.needs_ocr(doc, 0), "saved file has its text"
            for a, b in zip(_find(doc, "quick brown"), preview):
                assert all(abs(x - y) < 0.5 for x, y in zip(a, b)), f"saved {a} vs preview {b}"
            assert _render(doc, 0) == before, "nothing visible changed in the saved page"


def test_scan_preview_and_saved_text_agree():
    _check_scan(sideways=False)


def test_sideways_stored_scan():
    _check_scan(sideways=True)


def test_undo_drops_the_text_layer():
    ocr = _ocr()
    with tempfile.TemporaryDirectory() as tmp:
        scan = Path(tmp, "scan.pdf")
        _make_scan(scan)
        with PdfDocument(scan) as doc:
            words = ocr.recognize_page(doc, 0)
            doc.ocr = OcrEdits({doc.page_key(0): words})
            assert doc.find_text(0, "invoice")
            doc.ocr = OcrEdits()
            assert doc.char_count(0) == 0 and not doc.find_text(0, "invoice")


def test_recognize_text_in_the_app():
    _ocr()
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from pdfapp.mainwindow import MainWindow
    from pdfapp.settings import Settings
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        scan, text = Path(tmp, "scan.pdf"), Path(tmp, "text.pdf")
        _make_scan(scan)
        _text_pdf(text)
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        try:
            w.view.set_document(PdfDocument(scan))
            assert "looks scanned" in w.statusBar().currentMessage(), "the hint on open"
            f = w.view.document.add_source(text)  # a text page alongside: left alone
            w._edit(w.view.document.pages.inserted(w.view.document.source_refs(f), 1),
                    "Insert", 0, [0])
            w.recognize_text()
            app.processEvents()
            doc = w.view.document
            assert w.history.undo_label() == "Recognize Text"
            assert doc.find_text(0, "invoice number"), "the scan is searchable"
            assert doc.ocr.get(doc.page_key(1)) is None, "a page with text isn't OCR'd"
            assert "1 already had text" in w.statusBar().currentMessage()
            w.undo()
            assert not w.view.document.find_text(0, "invoice number"), "undone"
            w.redo()
            assert w.view.document.find_text(0, "invoice number")
        finally:
            w.view.set_document(None)
            w.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Skip as why:
            print(f"SKIP  {fn.__name__}: {why}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
