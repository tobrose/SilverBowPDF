"""The Help menu (guide, Check for Updates, About), the page-number box
fitting its page count, and Ctrl+W closing a tab.

Check for Updates runs its real network code against file:// URLs standing
in for GitHub -- never the real site. UI parts run on the offscreen platform
with INI settings -- never the real registry.

Run with pytest, or directly:  python tests/test_help.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pikepdf  # noqa: E402
from PySide6.QtCore import QSettings, Qt, QUrl  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pdfapp import __version__, updates  # noqa: E402
from pdfapp.guide import GUIDE_HTML  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402
from patching import patched  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _pdf(path, pages):
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
    pdf.save(path)
    return Path(path)


@contextmanager
def _window():
    from pdfapp.mainwindow import MainWindow
    with tempfile.TemporaryDirectory() as tmp:
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        w.resize(900, 700)
        w.show()
        try:
            yield w, Path(tmp)
        finally:
            for tab in w.tab_list():
                tab.view.set_document(None)
            w.close()


# ---- the page-number box --------------------------------------------------

def test_page_box_fits_the_page_count():
    with _window() as (w, tmp):
        widths = {}
        for pages in (9, 12, 150, 1200):
            w.open_path(_pdf(tmp / f"{pages}.pdf", pages))
            spin = w.page_spin
            spin.setValue(pages)  # the widest number it must show
            widths[pages] = spin.width()
            assert spin.width() >= spin.sizeHint().width(), \
                f"{pages} pages: box {spin.width()}px, needs {spin.sizeHint().width()}px"
            # The digits themselves fit beside the arrows.
            digits = spin.fontMetrics().horizontalAdvance(str(pages))
            assert spin.lineEdit().width() >= digits, f"{pages}: digits cut off"
        assert widths[9] <= widths[12] < widths[150] < widths[1200], widths
        # Back to the 9-page tab: the box shrinks again.
        w.tabs.setCurrentIndex(0)
        assert w.page_spin.width() == widths[9]
        # Windows' text size changed while running: the box follows.
        w.tabs.setCurrentIndex(3)
        font = _app.font()
        try:
            bigger = type(font)(font)
            bigger.setPointSizeF(font.pointSizeF() * 1.5)
            _app.setFont(bigger)
            for _ in range(5):
                _app.processEvents()
            assert w.page_spin.width() == w.page_spin.sizeHint().width() > widths[1200]
        finally:
            _app.setFont(font)
            for _ in range(5):
                _app.processEvents()


# ---- Ctrl+W ---------------------------------------------------------------

def test_ctrl_w_closes_the_tab():
    with _window() as (w, tmp):
        keys = [k.toString() for k in w.act_close.shortcuts()]
        assert "Ctrl+W" in keys and "Ctrl+F4" in keys, keys
        w.open_path(_pdf(tmp / "a.pdf", 1))
        w.open_path(_pdf(tmp / "b.pdf", 1))
        for _ in range(5):  # let the new tab's layout and focus settle
            _app.processEvents()
        w.activateWindow()
        w.view.setFocus()
        _app.processEvents()
        QTest.keyClick(w.view, Qt.Key_W, Qt.ControlModifier)  # the key, not the action
        _app.processEvents()
        assert w.tabs.count() == 1 and w.view.document.path.name == "a.pdf"


# ---- Help > Guide -----------------------------------------------------------

def test_guide_opens_from_the_menu_and_f1():
    with _window() as (w, tmp):
        assert [k.toString() for k in w.act_guide.shortcuts()] == ["F1"]
        w.act_guide.trigger()
        guide = w._guide
        assert guide is not None and guide.isVisible() and not guide.isModal()
        text = guide.browser.toPlainText()
        for heading in ("Opening and tabs", "Pages", "Signatures", "Forms", "Single-letter"):
            assert heading in text, heading
        w.act_guide.trigger()  # again: the same window, not a second one
        assert w._guide is guide


def test_every_shortcut_in_the_guide_is_real():
    """Key combinations the guide names must be bound to a menu action, or
    be one of the in-place keys handled outside menus (listed here)."""
    handled_elsewhere = {"Ctrl+Enter", "Shift+Tab", "Shift+Enter", "Ctrl+Shift+Tab"}
    with _window() as (w, tmp):
        from PySide6.QtGui import QAction
        bound = {k.toString() for a in w.findChildren(QAction) for k in a.shortcuts()}
        bound |= {keys.toString() for keys in w.letter_actions.values()}
        text = re.sub(r"<[^>]+>", " ", GUIDE_HTML)
        combos = set(re.findall(r"\b(?:Ctrl|Shift|Alt)(?:\+(?:Ctrl|Shift|Alt))*\+"
                                r"(?:PgUp|PgDn|Home|End|Tab|Enter|Up|Down|F\d+|[A-Z0-9]\b|[+\-=])",
                                text))
        combos |= set(re.findall(r"\bF\d+\b", text))
        assert len(combos) > 20, sorted(combos)
        aliases = {"Ctrl+PgDn": "Ctrl+PgDown"}
        missing = sorted(c for c in combos
                         if aliases.get(c, c) not in bound and c not in handled_elsewhere)
        assert not missing, f"the guide names keys nothing is bound to: {missing}"


# ---- Help > Check for Updates ---------------------------------------------

def _release_file(tmp, tag):
    path = Path(tmp, f"release-{tag}.json")
    path.write_text(json.dumps({"tag_name": tag, "html_url": "https://example.invalid/r",
                                "body": "notes"}), encoding="utf-8")
    return QUrl.fromLocalFile(str(path)).toString()


def _boxes():
    seen = []

    class Box(QMessageBox):
        @staticmethod
        def information(parent, title, text, *a, **k):
            seen.append(("information", text))
            return QMessageBox.Ok

        @staticmethod
        def warning(parent, title, text, *a, **k):
            seen.append(("warning", text))
            return QMessageBox.Ok

        @staticmethod
        def question(parent, title, text, *a, **k):
            seen.append(("question", text))
            return QMessageBox.Close
    return Box, seen


def _run_check(w):
    w.act_check_updates.trigger()  # Help > Check for Updates
    assert not w.act_check_updates.isEnabled(), "one check at a time"
    for _ in range(500):
        if w._update_check is None:
            break
        QTest.qWait(10)
    assert w._update_check is None, "the check never finished"
    assert w.act_check_updates.isEnabled()


def test_check_for_updates_through_the_menu():
    with _window() as (w, tmp):
        Box, seen = _boxes()
        with patched(QMessageBox=Box):
            with patched(RELEASES_URL=_release_file(tmp, "v99.0.0")):
                _run_check(w)
            assert seen[-1][0] == "question" and "99.0.0 is available" in seen[-1][1], seen
            with patched(RELEASES_URL=_release_file(tmp, f"v{__version__}")):
                _run_check(w)
            assert seen[-1][0] == "information" and "latest version" in seen[-1][1], seen
            missing = QUrl.fromLocalFile(str(tmp / "nothing-here.json")).toString()
            with patched(RELEASES_URL=missing):
                _run_check(w)
            assert seen[-1][0] == "warning" and "Couldn't check" in seen[-1][1], seen


def test_release_versions():
    assert updates.version_tuple("v0.8.1") == (0, 8, 1)
    assert updates.version_tuple("1.10") > updates.version_tuple("1.9.9")
    release = updates.parse_release(b'{"tag_name": "v1.2.3", "html_url": "u", "body": "b"}')
    assert release.version == "1.2.3" and release.page == "u"
    assert updates.newer_than_running(release, "1.2.2")
    assert not updates.newer_than_running(release, "1.2.3")
    redirect = "https://github.com/tobrose/SilverBowPDF/releases/tag/v0.10.0"
    assert updates.release_from_redirect(redirect) == updates.Release("0.10.0", redirect)
    # Not plain versions -- a release candidate is not "1.0.0" -- nor releases at all.
    for bad in (b"not json", b"{}", b'{"tag_name": "latest"}', b'{"tag_name": "v1.0.0-rc1"}'):
        try:
            updates.parse_release(bad)
            raise AssertionError(f"accepted {bad!r}")
        except updates.UpdateCheckError:
            pass
    try:
        updates.release_from_redirect("https://github.com/tobrose/SilverBowPDF/releases")
        raise AssertionError("no tag, but a release")
    except updates.UpdateCheckError as exc:
        assert "no published release" in str(exc)


@contextmanager
def _fake_github(status, headers):
    """A local web server answering every GET as GitHub might."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Answer(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/tobrose/SilverBowPDF/releases/latest"
    finally:
        server.shutdown()


def test_the_check_reads_githubs_redirect_and_explains_refusals():
    """How the real GitHub answers: a redirect to the latest release's
    page (not the rate-limited API), or 429/403 when it's had too many."""
    with _window() as (w, tmp):
        Box, seen = _boxes()
        with patched(QMessageBox=Box):
            with _fake_github(302, {"Location": "/tobrose/SilverBowPDF/releases/tag/v99.1.0"}) as url:
                with patched(RELEASES_URL=url):
                    _run_check(w)
            assert seen[-1][0] == "question" and "99.1.0 is available" in seen[-1][1], seen
            with _fake_github(429, {}) as url, patched(RELEASES_URL=url):
                _run_check(w)
            assert seen[-1][0] == "warning" and "too many recently" in seen[-1][1], seen
            with _fake_github(302, {"Location": "/tobrose/SilverBowPDF/releases"}) as url:
                with patched(RELEASES_URL=url):
                    _run_check(w)
            assert seen[-1][0] == "warning" and "no published release" in seen[-1][1], seen


# ---- Help > About -----------------------------------------------------------

def test_about_shows_the_version_and_the_error_log():
    from pdfapp import errors
    from pdfapp.helpflow import AboutDialog, about_html
    html = about_html()
    assert f"SilverbowPDF {__version__}" in html and str(errors.LOG) in html
    with _window() as (w, tmp):
        shown = []

        class Recorder(AboutDialog):
            def exec(self):
                shown.append(self)
                return 0
        with patched(AboutDialog=Recorder):
            w.act_about.trigger()
        assert shown, "Help > About opened nothing"


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
