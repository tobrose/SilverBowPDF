"""Ctrl + mouse wheel zooms around the cursor, by how far the wheel turned.

Two bugs this holds down. A notch arriving as several small events (smooth
mice, touchpads, remote-desktop clients) zoomed 15% per event -- 3x a
notch -- so the view lurched, worst past ~200% where renders are slow. And
the point under the cursor drifted with every step (the zoom scaled the
scroll position, but page margins and gaps don't scale).

UI parts run on the offscreen platform with INI settings -- never the real
registry.

Run with pytest, or directly:  python tests/test_zoom.py
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
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp.settings import Settings  # noqa: E402

_app = QApplication.instance() or QApplication([])


@contextmanager
def _window(pages=20):
    from pdfapp.mainwindow import MainWindow
    with tempfile.TemporaryDirectory() as tmp:
        pdf = pikepdf.Pdf.new()
        for _ in range(pages):
            pdf.add_blank_page(page_size=(612, 792))
        pdf.save(Path(tmp, "a.pdf"))
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        w.resize(1100, 800)
        w.show()
        try:
            w.open_path(Path(tmp, "a.pdf"))
            _settle()
            w.view.verticalScrollBar().setValue(3000)  # mid-document: a jump would show
            _settle()
            yield w
        finally:
            for tab in w.tab_list():
                tab.view.set_document(None)
            w.close()


def _settle():
    for _ in range(5):
        _app.processEvents()


_MOUSE = {}


def _cursor(view) -> QPoint:
    """Where the mouse is, in the viewport. Fixed on screen, like a real
    mouse: when zooming brings in a horizontal scrollbar the viewport gets
    shorter, and a point re-derived from its height would move."""
    vp = view.viewport()
    if view not in _MOUSE:
        _MOUSE.clear()
        _MOUSE[view] = vp.mapToGlobal(QPoint(vp.width() * 3 // 4, vp.height() // 3))
    return vp.mapFromGlobal(_MOUSE[view])  # off-centre: drift shows both ways


def _wheel(view, delta: int, ctrl: bool = True) -> None:
    vp = view.viewport()
    pos = _cursor(view)
    event = QWheelEvent(QPointF(pos), QPointF(vp.mapToGlobal(pos)), QPoint(0, 0),
                        QPoint(0, delta), Qt.NoButton,
                        Qt.ControlModifier if ctrl else Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(vp, event)


def _under_cursor(view):
    target = view.viewport().mapToGlobal(_cursor(view))
    for page in view._pages:
        local = page.mapFromGlobal(target)
        if page.rect().contains(local):
            return (page.index, *page.pdf_point_at(local))
    raise AssertionError("no page under the cursor")


def test_a_notch_is_fifteen_percent_however_it_arrives():
    with _window() as w:
        view = w.view
        start = view.scale
        _wheel(view, 120)  # an ordinary mouse: one event per notch
        _settle()
        assert abs(view.scale / start - 1.15) < 1e-6, view.scale / start
        before = view.scale
        for _ in range(8):  # a smooth mouse: the same notch as 8 events
            _wheel(view, 15)
        _settle()
        assert abs(view.scale / before - 1.15) < 1e-6, f"one notch zoomed {view.scale / before:.2f}x"
        before = view.scale
        _wheel(view, -120)
        _settle()
        assert abs(before / view.scale - 1.15) < 1e-6, "and back out the same"


def test_the_point_under_the_cursor_stays_put():
    with _window() as w:
        view = w.view
        start = _under_cursor(view)
        for step in range(14):  # 128% to ~900%, past where it used to go wrong
            _wheel(view, 120)
            _settle()
            page, x, y = _under_cursor(view)
            assert page == start[0], f"step {step + 1}: now over page {page + 1}"
            assert abs(x - start[1]) < 1.0 and abs(y - start[2]) < 1.0, \
                f"step {step + 1} at {view.scale:.0%}: drifted to ({x:.1f}, {y:.1f}) " \
                f"from ({start[1]:.1f}, {start[2]:.1f})"
        assert view.scale > 8.0
        for step in range(10):  # and back out
            _wheel(view, -120)
            _settle()
            page, x, y = _under_cursor(view)
            assert page == start[0] and abs(y - start[2]) < 1.0, f"out, step {step + 1}"


def test_the_wheel_without_ctrl_only_scrolls():
    with _window() as w:
        view = w.view
        scale, scroll = view.scale, view.verticalScrollBar().value()
        _wheel(view, -120, ctrl=False)
        _settle()
        assert view.scale == scale and view.verticalScrollBar().value() > scroll


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
