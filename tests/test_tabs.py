"""Tabs: one document per tab in one window, and a second launch handing its
files to the running window (instance.py) instead of starting another copy.

UI parts run on the offscreen platform with INI settings -- never the real
registry. Instance tests use their own pipe names, never the real one.

Run with pytest, or directly:  python tests/test_tabs.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pikepdf  # noqa: E402
from pikepdf import Dictionary, Name  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from pdfapp import instance  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402
from patching import patched  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _pdf(path, pages, label, password=None):
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    for i in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        page.Contents = pdf.make_stream(
            f"BT /F1 24 Tf 72 700 Td ({label} page {i + 1}) Tj ET".encode())
    if password:
        pdf.save(path, encryption=pikepdf.Encryption(user=password, owner=password))
    else:
        pdf.save(path)
    return Path(path)


def _settle():
    for _ in range(5):
        _app.processEvents()


def _answering(answer):
    """QMessageBox whose warnings (the save prompts) all answer `answer`."""
    asked = []

    class Box(QMessageBox):
        @staticmethod
        def warning(parent, title, text, *a, **k):
            asked.append(text)
            return answer
    Box.asked = asked
    return Box


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


def test_each_document_opens_in_its_own_tab():
    with _window() as (w, tmp):
        assert w.tabs.count() == 1 and w.view.document is None
        assert not w.tabs.tabBar().isVisible(), "no tab strip for an empty window"
        a, b = _pdf(tmp / "a.pdf", 2, "A"), _pdf(tmp / "b.pdf", 5, "B")
        w.open_path(a)
        assert w.tabs.count() == 1, "the empty start tab is used, not left behind"
        w.open_path(b)
        _settle()
        assert w.tabs.count() == 2 and w.view.document.path == b
        assert w.tabs.tabBar().isVisible()
        assert [w.tabs.tabText(i) for i in range(2)] == ["a.pdf", "b.pdf"]
        assert "b.pdf" in w.windowTitle() and w.page_total.text().strip() == "of 5"
        assert w.sidebar_stack.currentWidget() is w.current.sidebar
        assert w.current.sidebar.count() == 5
        w.act_prev_tab.trigger()  # Ctrl+Shift+Tab
        _settle()
        assert w.view.document.path == a and "a.pdf" in w.windowTitle()
        assert w.page_total.text().strip() == "of 2" and w.current.sidebar.count() == 2
        assert w.sidebar_stack.currentWidget() is w.current.sidebar
        w.act_next_tab.trigger()
        assert w.view.document.path == b
        # Opening an open file again goes to its tab.
        w.open_path(a)
        assert w.tabs.count() == 2 and w.view.document.path == a


def test_tabs_keep_their_own_edits_and_undo():
    with _window() as (w, tmp):
        a, b = _pdf(tmp / "a.pdf", 3, "A"), _pdf(tmp / "b.pdf", 3, "B")
        w.open_path(a)
        tab_a = w.current
        w.rotate_pages([0], 90)
        w.open_path(b)
        tab_b = w.current
        assert tab_a.dirty and not tab_b.dirty
        assert w.tabs.tabText(0) == "a.pdf •" and w.tabs.tabText(1) == "b.pdf"
        assert not w.act_undo.isEnabled() and not w.act_save.isEnabled(), "b has nothing to undo"
        w.tabs.setCurrentWidget(tab_a)
        assert w.act_undo.isEnabled() and w.act_undo.text() == "&Undo Rotate Page 1 Right"
        w.act_undo.trigger()
        assert not tab_a.dirty and tab_a.document.page_rotation(0) == 0
        assert tab_b.document.page_rotation(0) == 0


def test_closing_a_tab_asks_to_save_its_changes():
    with _window() as (w, tmp):
        a, b = _pdf(tmp / "a.pdf", 2, "A"), _pdf(tmp / "b.pdf", 2, "B")
        w.open_path(a)
        w.open_path(b)
        w.rotate_pages([1], -90)
        cancel = _answering(QMessageBox.Cancel)
        with patched(QMessageBox=cancel):
            assert not w.close_tab(1)  # its close button
        assert w.tabs.count() == 2 and cancel.asked and "b.pdf" in cancel.asked[0]
        discard = _answering(QMessageBox.Discard)
        with patched(QMessageBox=discard):
            w.act_close.trigger()  # Ctrl+W
        assert w.tabs.count() == 1 and w.view.document.path == a
        # The last tab closes to the empty start page, not to no window.
        w.act_close.trigger()
        assert w.tabs.count() == 1 and w.view.document is None
        assert not w.tabs.tabBar().isVisible() and w.windowTitle() == "SilverbowPDF"


def test_closing_the_window_asks_for_every_unsaved_tab():
    with _window() as (w, tmp):
        for name in ("a", "b", "c"):
            w.open_path(_pdf(tmp / f"{name}.pdf", 2, name.upper()))
            if name != "b":
                w.rotate_pages([0], 90)
        cancel = _answering(QMessageBox.Cancel)
        with patched(QMessageBox=cancel):
            event = QCloseEvent()
            w.closeEvent(event)
        assert not event.isAccepted() and len(cancel.asked) == 1 and "a.pdf" in cancel.asked[0]
        assert w.view.document.path.name == "a.pdf", "the tab asking is the one shown"
        discard = _answering(QMessageBox.Discard)
        with patched(QMessageBox=discard):
            event = QCloseEvent()
            w.closeEvent(event)
        assert event.isAccepted() and [t[:5] for t in discard.asked] == ["Save "] * 2
        assert all(tab.document is None for tab in w.tab_list())


def test_a_cancelled_open_leaves_no_tab_behind():
    with _window() as (w, tmp):
        a = _pdf(tmp / "a.pdf", 2, "A")
        locked = _pdf(tmp / "locked.pdf", 1, "L", password="pw")
        w.open_path(a)

        class NoPassword(QInputDialog):
            @staticmethod
            def getText(*args, **kwargs):
                return "", False
        with patched(QInputDialog=NoPassword):
            w.open_path(locked)
        assert w.tabs.count() == 1 and w.view.document.path == a


def test_a_background_tab_gives_back_its_memory():
    with _window() as (w, tmp):
        a, b = _pdf(tmp / "a.pdf", 4, "A"), _pdf(tmp / "b.pdf", 4, "B")
        w.open_path(a)
        _settle()
        tab_a = w.current
        assert tab_a.view.renderer.cached_bytes > 0 and tab_a.document._loaded
        w.open_path(b)
        _settle()
        assert tab_a.view.renderer.cached_bytes == 0 and not tab_a.document._loaded
        w.tabs.setCurrentWidget(tab_a)
        _settle()
        assert tab_a.view.renderer.cached_bytes > 0, "back on screen, it renders again"
        assert tab_a.document.text_in(0, 0, 5) == "A page"


def test_style_choices_follow_from_tab_to_tab():
    with _window() as (w, tmp):
        w.open_path(_pdf(tmp / "a.pdf", 1, "A"))
        w._set_highlight_color((0.5, 1.0, 0.5))
        w._set_shape_style(width=6)
        w.open_path(_pdf(tmp / "b.pdf", 1, "B"))
        assert w.view.highlight_color == (0.5, 1.0, 0.5) and w.view.shape_width == 6.0


# ---- one running copy per user session --------------------------------

def _name() -> str:
    return f"SilverbowPDF-test-{uuid.uuid4().hex}"


_CLIENT = ("import sys; from PySide6.QtCore import QCoreApplication; app = QCoreApplication([]); "
           "from pdfapp import instance; "
           "sys.exit(0 if instance.send_to_running(sys.argv[2:], sys.argv[1], 5000) else 3)")


def _send(paths, name):
    """send_to_running from a second process, as a second launch is: it
    blocks while the running app answers from its event loop. (Not a
    thread: Windows' pipe reading needs a proper Qt thread, which a second
    launch's main thread is.)"""
    root = os.path.join(os.path.dirname(__file__), "..")
    client = subprocess.Popen([sys.executable, "-c", _CLIENT, name, *paths], cwd=root)
    while client.poll() is None:
        _app.processEvents()
    return client.returncode == 0


def test_the_pipe_is_per_user_and_session():
    import getpass
    name = instance.server_name()
    assert name.startswith("SilverbowPDF-") and name.count("-") >= 2
    user = "".join(c if c.isalnum() or c in "_.-" else "_" for c in getpass.getuser())
    assert user in name and name.rsplit("-", 1)[1].isdigit(), name


def test_handover_to_a_running_copy():
    name = _name()
    assert not instance.send_to_running(["x.pdf"], name, timeout_ms=500), "nobody listening"
    server = instance.InstanceServer(name)
    try:
        assert server.listening
        received = []
        server.files_requested.connect(received.append)
        assert _send(["some file.pdf", "Résumé.pdf"], name)
        _settle()
        assert [Path(p).name for p in received[0]] == ["some file.pdf", "Résumé.pdf"]
        assert all(Path(p).is_absolute() for p in received[0])
        assert _send([], name), "a plain launch only brings the window forward"
        _settle()
        assert received[1] == []
    finally:
        server.close()


def test_files_go_only_to_a_copy_run_by_this_user():
    # Windows lets anyone listen under a pipe name already in use, so the
    # newcomer checks who it's talking to before sending a file name.
    from PySide6.QtNetwork import QLocalSocket
    name = _name()
    server = instance.InstanceServer(name)
    try:
        socket = QLocalSocket()
        socket.connectToServer(name)
        assert socket.waitForConnected(3000)
        _settle()
        assert instance._server_is_ours(socket) == os.getpid()
        socket.abort()
        assert instance._user_sid(os.getpid()) is not None
        assert instance._user_sid(4) != instance._user_sid(os.getpid()), "System isn't us"
        received = []
        server.files_requested.connect(received.append)
        real = instance._user_sid
        try:
            # As if the listener were another user's process.
            instance._user_sid = lambda pid: b"me" if pid == os.getpid() else b"someone else"
            assert not instance.send_to_running(["secret.pdf"], name, timeout_ms=1000)
        finally:
            instance._user_sid = real
        _settle()
        assert received == [], "not a word to someone else's pipe"
    finally:
        server.close()


def test_a_second_launch_opens_in_a_new_tab():
    name = _name()
    with _window() as (w, tmp):
        server = instance.InstanceServer(name)
        server.files_requested.connect(w.open_paths)
        try:
            w.open_path(_pdf(tmp / "a.pdf", 1, "A"))
            b = _pdf(tmp / "b.pdf", 2, "B")
            assert _send([str(b)], name), "handover"
            _settle()
            assert w.tabs.count() == 2, f"{w.tabs.count()} tabs"
            # Same file, not same spelling: the newcomer sends absolute paths,
            # and a short (8.3) temp folder like GitHub's comes back long.
            assert os.path.samefile(w.view.document.path, b), w.view.document.path
        finally:
            server.close()


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
