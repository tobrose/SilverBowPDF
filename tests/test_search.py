"""Viewer UI: find (Ctrl+F), the Pan tool, the mark bar under a text
selection, and the absence of view rotation.

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
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.pageview import Tool  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402
from patching import patched  # noqa: E402

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


def _select_by_drag(view, needle):
    """Drag across `needle` on page 1 like a user would; return the page."""
    page = view._pages[0]
    rect = view.document.find_text(0, needle)[0][2][0]
    left, top, right, bottom = page.widget_transform().pdf_rect_to_widget(rect)
    mid = int((top + bottom) / 2)
    _drag(view, page, Qt.LeftButton, QPoint(int(left) + 1, mid), QPoint(int(right) - 1, mid))
    _app.processEvents()
    return page


def _bar_button(view, label):
    from PySide6.QtWidgets import QToolButton
    return next(b for b in view._mark_bar.findChildren(QToolButton) if b.text() == label)


def test_mark_bar_under_a_text_selection():
    with _window() as w:
        # Wide: the offscreen platform has no fonts, and its stand-in glyph
        # boxes make the bar far wider than it is on Windows.
        w.resize(1600, 800)
        _settle()
        view = w.view
        page = _select_by_drag(view, "beta gamma")
        assert view.selected_text() == "beta gamma", view.selected_text()
        bar = view._mark_bar
        assert bar is not None and bar.isVisible(), "bar appears after a text drag"
        area = view._container
        assert bar.parent() is area and area.rect().contains(bar.geometry()), "inside the column"
        sel = view._widget_rect(page, view.document.find_text(0, "beta gamma")[0][2][0])
        sel.translate(QPointF(page.pos()))
        assert 0 < bar.geometry().top() - sel.bottom() < 12, "just under the selection"

        _bar_button(view, "Underline").click()
        _app.processEvents()
        kinds = [a.kind for a in view.document.page_annots(0)]
        assert kinds == ["Underline"], kinds
        assert view._mark_bar is None and view.text_selection() is None
        assert w.history.can_undo(), "one undoable edit"

        _select_by_drag(view, "Fox")
        _bar_button(view, "Highlight").click()
        assert view._mark_bar is None
        assert [a.kind for a in view.document.page_annots(0)] == ["Underline", "Highlight"]

        _select_by_drag(view, "here")
        assert view._mark_bar is not None
        blank = QPoint(page.width() // 2, page.height() - 20)  # a click on empty page
        _drag(view, page, Qt.LeftButton, blank, blank)
        assert view._mark_bar is None and view.text_selection() is None, "click away dismisses"

        _select_by_drag(view, "here")
        view.set_scale(view.scale * 1.5)
        assert view._mark_bar is None, "zooming dismisses (its position is stale)"

        view.set_tool(Tool.HIGHLIGHT)
        _select_by_drag(view, "Alpha")
        assert view._mark_bar is None, "the Highlight tool marks directly, no bar"
        assert view.document.page_annots(0)[-1].kind == "Highlight"


def _bar_button_tip(bar, tip):
    from PySide6.QtWidgets import QToolButton
    return next(b for b in bar.findChildren(QToolButton) if b.toolTip() == tip)


def test_mark_bar_colour_swatches():
    from pdfapp.annots import HIGHLIGHT_COLORS
    with _window() as w:
        view = w.view
        _select_by_drag(view, "beta gamma")
        _bar_button_tip(view._mark_bar, "Highlight green").click()
        _app.processEvents()
        (mark,) = view.document.page_annots(0)
        assert mark.kind == "Highlight" and mark.color == HIGHLIGHT_COLORS["Green"], mark.color
        assert view.highlight_color == HIGHLIGHT_COLORS["Green"], "the pick becomes current"

        _select_by_drag(view, "Fox")
        _bar_button(view, "Highlight").click()  # current colour, now green
        assert view.document.page_annots(0)[-1].color == HIGHLIGHT_COLORS["Green"]


def test_typewriter_style_bar():
    from pdfapp.annots import TEXT_COLORS
    with _window() as w:
        view = w.view
        page = view._pages[0]
        view.set_tool(Tool.TYPEWRITER)
        spot = QPoint(150, 300)
        _drag(view, page, Qt.LeftButton, spot, spot)
        ed = view._editor
        assert ed is not None and ed["bar"].isVisible(), "style bar shows while typing"
        bar = ed["bar"]
        assert bar.geometry().bottom() < ed["widget"].geometry().translated(page.pos()).top(), \
            "above the text"
        ed["widget"].insertPlainText("Hello there")

        _bar_button_tip(bar, "Red").click()
        _bar_button_tip(bar, "18 pt").click()
        assert view._editor is ed, "picking a style doesn't end typing"
        assert ed["color"] == TEXT_COLORS["Red"] and ed["size"] == 18
        assert "rgb(204,26,26)" in ed["widget"].styleSheet(), "the editor shows the colour"
        assert w.act_tool_typewriter.icon().pixmap(14, 14).toImage().pixelColor(7, 7).red() > 150
        assert [a.text() for a in w.text_size_actions.actions() if a.isChecked()] == ["18 pt"]

        view.commit_text_edit()
        _app.processEvents()
        (typed,) = view.document.page_annots(0)
        assert typed.kind == "FreeText" and typed.contents == "Hello there"
        assert typed.color == TEXT_COLORS["Red"] and typed.font_size == 18
        assert not bar.isVisible() and view._editor is None
        assert (view.text_color, view.text_size) == (TEXT_COLORS["Red"], 18), "sticky default"

        # Restyling existing text without changing a word is still an edit.
        view._edit_text(page, typed)
        _bar_button_tip(view._editor["bar"], "Blue").click()
        view.commit_text_edit()
        _app.processEvents()
        (again,) = view.document.page_annots(0)
        assert again.color == TEXT_COLORS["Blue"] and again.contents == "Hello there"
        assert w.history.undo_label() == "Edit Text"


def test_text_box_tool_is_gone():
    from PySide6.QtGui import QAction
    with _window() as w:
        texts = [a.text() for a in w.findChildren(QAction)]
        assert "Text Box" not in texts and "Typewriter" in texts
        assert "Text Field" in texts and "Multi-line Text Field" in texts, "form tools stay"
        assert not hasattr(Tool, "TEXTBOX")


def test_every_menu_action_runs():
    """Every QAction in the window, triggered with a document open, as a
    click or shortcut would. Windows' and Qt's dialogs are stand-ins that
    cancel. An exception in an action's slot goes to sys.excepthook --
    which in the installed app (no console) used to mean "does nothing";
    that's how File > Print shipped broken. Caught here instead."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMessageBox

    class Box(QMessageBox):
        @staticmethod
        def warning(*a, **k): return QMessageBox.Cancel
        critical = information = question = warning

    class Files(QFileDialog):
        @staticmethod
        def getOpenFileName(*a, **k): return "", ""
        @staticmethod
        def getOpenFileNames(*a, **k): return [], ""
        @staticmethod
        def getSaveFileName(*a, **k): return "", ""

    class Inputs(QInputDialog):
        @staticmethod
        def getText(*a, **k): return "", False
        @staticmethod
        def getMultiLineText(*a, **k): return "", False
        @staticmethod
        def getInt(*a, **k): return 0, False

    class Cancelled:
        choice = None
        def __init__(self, *a, **k): pass
        def __getattr__(self, name): return lambda *a, **k: None
        def exec(self): return QDialog.Rejected

    errors = []
    saved_hook = sys.excepthook
    sys.excepthook = lambda *exc: errors.append(exc)
    stand_ins = dict(QMessageBox=Box, QFileDialog=Files, QInputDialog=Inputs,
                     QPrintDialog=Cancelled, PrintPreview=Cancelled, ProtectionDialog=Cancelled,
                     SignDialog=Cancelled, CreateIDDialog=Cancelled, SignaturesDialog=Cancelled,
                     AboutDialog=Cancelled, choose_picture=lambda *a, **k: None,
                     # Check for Updates: a file that isn't there, never GitHub.
                     RELEASES_URL="file:///C:/no-such-dir/silverbowpdf-release.json")
    try:
        with patched(**stand_ins), _window() as w:
            skip = {w.act_quit, w.act_close}  # end the session; closing is tested elsewhere
            actions = [a for a in w.findChildren(QAction) if a not in skip and a.text()]
            ran = 0
            for act in actions:
                if not act.isEnabled() or w.view.document is None:
                    continue
                act.trigger()
                _app.processEvents()
                ran += 1
            assert ran > 30, f"only {ran} actions ran"
            for _ in range(500):  # Check for Updates answers later: still stubbed then
                if w._update_check is None:
                    break
                QTest.qWait(10)
            assert w._update_check is None, "Check for Updates never finished"
            w.close_document()  # last: Close, with the (cancelling) save prompt
    finally:
        sys.excepthook = saved_hook
    assert not errors, "\n".join(f"{e[0].__name__}: {e[1]}" for e in errors)


def test_rotate_view_is_gone():
    from PySide6.QtGui import QAction
    with _window() as w:
        texts = [a.text().replace("&", "") for a in w.findChildren(QAction)]
        assert not any("Rotate View" in t for t in texts), texts
        keys = {a.shortcut().toString() for a in w.findChildren(QAction)}
        assert not keys & {"Ctrl+L", "Ctrl+R"}, "view-rotation shortcuts removed"
        assert "Rotate Pages Right" in texts, "page rotation stays"
        assert not hasattr(w.view, "rotate_view") and w.view.rotation == 0


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
