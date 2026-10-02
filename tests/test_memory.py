"""Memory stays bounded however much of a document is looked at.

The target is a shared multi-session host with no GPU, where every user's copy of
the app shares one machine's RAM. Measured before these limits: an 80-page
300 DPI scan grew to 800MB once scrolled through (every pdfium page kept
open with its decoded image), and 800% zoom held 1.4GB of page images.
Opening any document also rendered every page once, off screen.

UI parts run on the offscreen platform with INI settings -- never the real
registry.

Run with pytest, or directly:  python tests/test_memory.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pikepdf  # noqa: E402
from pikepdf import Dictionary, Name  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp import document as document_module  # noqa: E402
from pdfapp.document import PAGE_CACHE_LIMIT, PdfDocument  # noqa: E402
from pdfapp.rendering import PageRenderer  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _make_pdf(path, pages: int) -> None:
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    for i in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        page.Contents = pdf.make_stream(
            f"0 0 1 rg 50 50 200 100 re f BT /F1 14 Tf 72 700 Td (Page {i + 1}) Tj ET".encode())
        if i % 3 == 1:
            page.Rotate = 90
    pdf.save(path)


def test_pdfium_pages_are_released():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "long.pdf")
        _make_pdf(path, 3 * PAGE_CACHE_LIMIT)
        with PdfDocument(path) as doc:
            renderer = PageRenderer(doc, max_pages=0)
            for i in range(doc.page_count):
                renderer.image(i, 0.5)
                assert doc.text_in(i, 0, doc.char_count(i) - 1) == f"Page {i + 1}"
                assert len(doc._loaded) <= PAGE_CACHE_LIMIT
            assert sum(len(s.page_cache) for s in doc._sources) == len(doc._loaded)
            assert sum(len(s.text_cache) for s in doc._sources) <= PAGE_CACHE_LIMIT
            # An evicted page comes back when asked for, text and all...
            assert 0 not in doc._sources[0].page_cache
            assert doc.text_in(0, 0, 5) == "Page 1" and doc.char_at(0, 80, 705) >= 0
            # ...but its size and rotation never reload it: widget_transform
            # asks for those on every paint and mouse move.
            loaded = list(doc._loaded)
            sizes = [(doc.page_box(i), doc.page_rotation(i)) for i in range(doc.page_count)]
            assert list(doc._loaded) == loaded
            assert sizes[1][1] == 90 and sizes[0][1] == 0


def test_render_cache_stays_within_budget():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "pages.pdf")
        _make_pdf(path, 12)
        page_bytes = lambda scale: PageRenderer(doc, max_pages=0).image(0, scale).sizeInBytes()  # noqa: E731
        with PdfDocument(path) as doc:
            small, big = page_bytes(0.5), page_bytes(2.0)
            renderer = PageRenderer(doc, max_bytes=2 * big)
            for i in (0, 2, 3):  # upright pages, all the same size
                renderer.image(i, 0.5)
            renderer.image(5, 2.0)
            renderer.image(6, 2.0)
            # Over budget: the other zoom level's images went first, oldest first.
            assert renderer.cached_bytes <= 2 * big
            assert renderer.cached(5, 2.0) is not None and renderer.cached(6, 2.0) is not None
            assert renderer.cached(0, 0.5) is None
            for i in (8, 9, 11):
                renderer.image(i, 2.0)
            assert renderer.cached_bytes <= 2 * big
            # Even alone over budget, the newest two stay: both may be on
            # screen at once, and dropping one would re-render every paint.
            tiny = PageRenderer(doc, max_bytes=small)
            tiny.image(0, 2.0)
            tiny.image(2, 2.0)
            tiny.image(3, 2.0)
            assert tiny.cached(2, 2.0) is not None and tiny.cached(3, 2.0) is not None
            assert tiny.cached(0, 2.0) is None and tiny.cached_bytes == 2 * big
            tiny.clear()
            assert tiny.cached_bytes == 0


def test_opening_renders_only_whats_on_screen():
    from pdfapp.mainwindow import MainWindow
    rendered = []
    original = document_module.PdfDocument.render_bitmap

    def counting(self, index, scale, *args, **kwargs):
        rendered.append((index, scale))
        return original(self, index, scale, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        w.resize(900, 700)
        w.show()
        path = Path(tmp, "forty.pdf")
        _make_pdf(path, 40)
        document_module.PdfDocument.render_bitmap = counting
        try:
            w.view.set_document(PdfDocument(path))
            for _ in range(5):
                _app.processEvents()
            pages = {i for i, scale in rendered if scale > 0.5}  # not the thumbnails
            assert 0 < len(pages) <= 3, f"rendered pages {sorted(pages)} of 40 on opening"
            assert len(rendered) < 15, rendered
            # A page edit rebuilds the view the same way: still only what's seen.
            rendered.clear()
            w.rotate_pages([0], 90)
            for _ in range(5):
                _app.processEvents()
            assert len({i for i, scale in rendered if scale > 0.5}) <= 3, rendered
        finally:
            document_module.PdfDocument.render_bitmap = original
            w.view.set_document(None)
            w.close()


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
