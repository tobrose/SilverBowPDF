"""Find (Ctrl+F) and the Pan tool.

Search is pdfium's, over displayed pages; the find bar scans in slices from
the current page and steps through matches in reading order, wrapping.
UI parts run on the offscreen platform with INI settings -- never the real
registry.

Run with pytest, or directly:  python tests/test_search.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pikepdf  # noqa: E402
from pikepdf import Dictionary, Name  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.pageview import Tool  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402

_app = QApplication.instance() or QApplication([])

# Page 3 (index 2) is blank on purpose: no text layer at all.
PAGES = [
    "Alpha beta gamma. The Fox is here.",
    "fox foxes Fox",
    None,
    "Another fox appears.",
]


def _make_pdf(path):
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    for text in PAGES:
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        if text:
            page.Contents = pdf.make_stream(f"BT /F1 14 Tf 72 700 Td ({text}) Tj ET".encode())
    pdf.save(path)


def _settle():
    # Layout requests chain across several event-loop passes; until they're
    # done the scroll range is too short and scrolling gets clamped.
    for _ in range(5):
        _app.processEvents()


@contextmanager
def _window():
    """A main window showing the test PDF. The document is closed however
    the test ends -- otherwise a failed assert surfaces as a locked temp file."""
    from pdfapp.mainwindow import MainWindow
    with tempfile.TemporaryDirectory() as tmp:
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        w.resize(900, 700)
        w.show()
        try:
            src = Path(tmp, "find.pdf")
            _make_pdf(src)
            w.view.set_document(PdfDocument(src))
            _settle()
            yield w
        finally:
            w.view.set_document(None)  # also drops the undo history: no save prompt
            w.close()


def _search(w, text):
    bar = w.find_bar
    bar.open()
    bar.edit.setText(text)
    bar._restart()
    while bar._scanner.isActive():
        _app.processEvents()
    _app.processEvents()
    return bar


def test_find_text():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "find.pdf")
        _make_pdf(src)
        with PdfDocument(src) as doc:
            counts = [len(doc.find_text(i, "fox")) for i in range(4)]
            assert counts == [1, 3, 0, 1], f"case-insensitive, substrings: {counts}"
            assert [len(doc.find_text(i, "fox", match_case=True)) for i in range(4)] == [0, 2, 0, 1]
            assert [len(doc.find_text(i, "fox", whole_word=True)) for i in range(4)] == [1, 2, 0, 1]
            assert doc.find_text(0, "") == []
            assert not any(s.text_cache for s in doc._sources), \
                "searching must not pin every page's text in memory"

            start, end, rects = doc.find_text(0, "fox")[0]
            assert doc.text_in(0, start, end) == "Fox", "same char indices as selection"
            assert rects == doc.text_rects(0, start, end)
            left, bottom, right, top = rects[0]
            assert 72 < left < right < 400 and 690 < bottom < top < 720, rects[0]
            assert len(doc.find_text(0, "fox")) == 1, "a page with cached text searches the same"

            doc.pages = doc.pages.deleted([0])  # displayed index 0 is now file page 2
            assert len(doc.find_text(0, "fox")) == 3


def test_find_bar_steps_in_reading_order_and_wraps():
    with _window() as w:
        bar = _search(w, "fox")
        assert bar.isVisible()
        assert bar.status.text() == "1 of 5", bar.status.text()
        pages = [w.view._search_current[0]]
        for _ in range(5):
            bar.find_next()
            pages.append(w.view._search_current[0])
        assert pages == [0, 1, 1, 1, 3, 0], f"reading order, then wrap: {pages}"
        bar.find_previous()
        assert w.view._search_current[0] == 3 and bar.status.text() == "5 of 5"
        assert sum(len(h) for h in w.view._search_hits.values()) == 5

        bar.case_box.setChecked(True)  # restarts the search
        while bar._scanner.isActive():
            _app.processEvents()
        assert bar.status.text().endswith("of 3"), bar.status.text()

        bar.edit.setText("zebra")
        bar._restart()
        while bar._scanner.isActive():
            _app.processEvents()
        assert bar.status.text() == "No matches"

        bar.close_bar()
        assert not bar.isVisible() and not w.view._search_hits


def test_find_starts_from_the_current_page():
    with _window() as w:
        w.view.scroll_to_page(3)
        _settle()
        assert w.view.current_page == 3
        _search(w, "fox")
        assert w.view._search_current[0] == 3, "the next match after where you're reading"


def test_page_edits_rescan_quietly():
    with _window() as w:
        bar = _search(w, "fox")
        w.delete_pages([0])
        for _ in range(3):  # the rescan is queued behind the rebuild
            _app.processEvents()
        while bar._scanner.isActive():
            _app.processEvents()
        assert sum(len(h) for h in w.view._search_hits.values()) == 4, "page 1's match is gone"
        assert w.view._search_current is None, "no jumping the view after an edit"
        assert bar.status.text() == "4 matches", bar.status.text()


def _mouse(kind, widget, pos, button, buttons):
    local = QPointF(pos)
    return QMouseEvent(kind, local, QPointF(widget.mapToGlobal(pos)), button, buttons,
                       Qt.NoModifier)


def _drag(view, page, button, start, end):
    view.page_mouse_press(page, _mouse(QEvent.MouseButtonPress, page, start, button, button))
    view.page_mouse_move(page, _mouse(QEvent.MouseMove, page, end, Qt.NoButton, button))
    view.page_mouse_release(page, _mouse(QEvent.MouseButtonRelease, page, end, button,
                                         Qt.NoButton))


def test_pan_tool_and_middle_drag_scroll():
    with _window() as w:
        view = w.view
        view.set_scale(3.0)
        _settle()
        page = view._pages[0]
        hbar, vbar = view.horizontalScrollBar(), view.verticalScrollBar()
        hbar.setValue(200)
        vbar.setValue(300)

        w.act_tool_pan.trigger()
        assert view.tool is Tool.PAN and page.cursor().shape() == Qt.OpenHandCursor
        _drag(view, page, Qt.LeftButton, QPoint(300, 300), QPoint(260, 220))
        assert (hbar.value(), vbar.value()) == (240, 380), "content follows the hand"
        assert QApplication.overrideCursor() is None, "closed hand released"
        assert view.selected_annot() is None and view.text_selection() is None

        view.set_tool(Tool.SELECT)
        _drag(view, page, Qt.MiddleButton, QPoint(300, 300), QPoint(330, 350))
        assert (hbar.value(), vbar.value()) == (210, 330), "middle drag pans in any tool"
        assert view.tool is Tool.SELECT


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
