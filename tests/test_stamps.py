"""Stamps resize: drag an edge or corner (Select tool), proportions kept.

A stamp's appearance is a picture -- a framed word, or a signature -- and
viewers map it onto the annotation's box, so a resized stamp is a stamp in
a new box: ours are redrawn there, other apps' keep their own appearance
and are just given the new /Rect. Preview and saved file must match pixel
for pixel, as for every edit.

UI parts run on the offscreen platform with INI settings -- never the real
registry.

Run with pytest, or directly:  python tests/test_stamps.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pikepdf  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp.annots import STAMPS  # noqa: E402
from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.pageview import Tool  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402
from test_annots import _differing_pixels, _shown  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _pdf(path, rotate=0):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    if rotate:
        pdf.pages[0].Rotate = rotate
    pdf.save(path)


@contextmanager
def _window(tmp, path):
    from pdfapp.mainwindow import MainWindow
    ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
    w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
    w.resize(900, 900)
    w.show()
    try:
        w.open_path(path)
        for _ in range(5):
            _app.processEvents()
        yield w
    finally:
        for tab in w.tab_list():
            tab.view.set_document(None)
        w.close()


def _mouse(view, kind, pos, button, buttons):
    page = view._pages[0]
    event = QMouseEvent(kind, QPointF(pos), QPointF(page.mapToGlobal(pos)), button, buttons,
                        Qt.NoModifier)
    {QEvent.MouseButtonPress: view.page_mouse_press, QEvent.MouseMove: view.page_mouse_move,
     QEvent.MouseButtonRelease: view.page_mouse_release}[kind](page, event)


def _drag(view, a: QPoint, b: QPoint):
    _mouse(view, QEvent.MouseButtonPress, a, Qt.LeftButton, Qt.LeftButton)
    mid = QPoint((a.x() + b.x()) // 2, (a.y() + b.y()) // 2)
    _mouse(view, QEvent.MouseMove, mid, Qt.NoButton, Qt.LeftButton)
    _mouse(view, QEvent.MouseMove, b, Qt.NoButton, Qt.LeftButton)
    _mouse(view, QEvent.MouseButtonRelease, b, Qt.LeftButton, Qt.NoButton)


def _stamp(doc):
    (stamp,) = [a for a in doc.page_annots(0) if a.kind == "Stamp"]
    return stamp


def _screen_box(view, annot):
    left, top, right, bottom = view._pages[0].widget_transform().pdf_rect_to_widget(annot.rect)
    return left, top, right, bottom


def _inked(image) -> int:
    """Pixels clearly not white: how much stamp there is on the page."""
    w, h, data = image
    return sum(1 for i in range(0, len(data), 4)
               if min(data[i], data[i + 1], data[i + 2]) < 200)


def _width_height(rect):
    x0, y0, x1, y1 = rect
    return x1 - x0, y1 - y0


def test_resizing_a_stamp_keeps_its_proportions_and_saves_as_shown():
    for rotate in (0, 90):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, f"stamped-{rotate}.pdf")
            _pdf(path, rotate)
            with _window(tmp, path) as w:
                view = w.view
                w._pick_stamp(STAMPS[0])  # Stamps menu > Approved
                _mouse(view, QEvent.MouseButtonPress, QPoint(300, 300), Qt.LeftButton,
                       Qt.LeftButton)
                _mouse(view, QEvent.MouseButtonRelease, QPoint(300, 300), Qt.LeftButton,
                       Qt.NoButton)
                assert view.tool is Tool.SELECT
                before = _stamp(view.document)
                small = _shown(view.document, 0)
                left, top, right, bottom = _screen_box(view, before)
                # The bottom-right corner, pulled out: proportions kept.
                _drag(view, QPoint(round(right), round(bottom)),
                      QPoint(round(right) + 160, round(bottom) + 10))
                after = _stamp(view.document)
                assert w.history.undo_label() == "Resize Stamp", w.history.undo_label()
                (w0, h0), (w1, h1) = _width_height(before.rect), _width_height(after.rect)
                assert w1 > w0 * 1.5, (before.rect, after.rect)
                assert abs(w1 / h1 - w0 / h0) < 0.02 * (w0 / h0), "proportions kept"
                # Opposite corner stays put (as shown on screen).
                l2, t2, _, _ = _screen_box(view, after)
                assert abs(l2 - left) < 1 and abs(t2 - top) < 1
                big = _shown(view.document, 0)
                grown = _inked(big) / _inked(small)
                assert grown > 2.2, f"the stamp itself got bigger, not just its box ({grown:.2f}x)"
                # Saved as shown.
                assert w.save()
                assert _differing_pixels(big, _shown(w.view.document, 0)) == 0
                saved = _stamp(w.view.document)
                assert all(abs(a - b) < 0.01 for a, b in zip(saved.rect, after.rect))


def test_a_stamp_already_in_the_file_resizes_too():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "file-stamp.pdf")
        _pdf(path)
        with _window(tmp, path) as w:
            w._pick_stamp(STAMPS[3])  # Draft
            _mouse(w.view, QEvent.MouseButtonPress, QPoint(300, 300), Qt.LeftButton,
                   Qt.LeftButton)
            _mouse(w.view, QEvent.MouseButtonRelease, QPoint(300, 300), Qt.LeftButton,
                   Qt.NoButton)
            assert w.save()  # now a stamp in the file, with its own appearance
            view = w.view
            original = _stamp(view.document)
            assert original.origin is not None
            with pikepdf.open(path) as pdf:
                ap_before = pdf.pages[0].Annots[0].AP.N.read_bytes()
            small = _shown(view.document, 0)
            left, top, right, bottom = _screen_box(view, original)
            _drag(view, QPoint(round(left), round(top)),
                  QPoint(round(left) - 120, round(top) - 40))  # top-left corner, outwards
            resized = _stamp(view.document)
            assert _width_height(resized.rect)[0] > _width_height(original.rect)[0] * 1.5
            big = _shown(view.document, 0)
            assert _inked(big) > 2.2 * _inked(small)
            assert w.save()
            assert _differing_pixels(big, _shown(w.view.document, 0)) == 0
            with pikepdf.open(path) as pdf:
                annot = pdf.pages[0].Annots[0]
                assert annot.AP.N.read_bytes() == ap_before, "its own appearance, unredrawn"
                assert all(abs(float(a) - b) < 0.01 for a, b in zip(annot.Rect, resized.rect))


def test_a_signature_picture_resizes_like_a_stamp():
    from test_signatures import _scanned_signature
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "signed.pdf")
        _pdf(path)
        picture = Path(tmp, "signature.png")
        _scanned_signature().save(str(picture))
        with _window(tmp, path) as w:
            w.settings.set_signature_picture(str(picture))
            w.act_sign_picture.trigger()
            _mouse(w.view, QEvent.MouseButtonPress, QPoint(250, 250), Qt.LeftButton,
                   Qt.LeftButton)
            _mouse(w.view, QEvent.MouseButtonRelease, QPoint(250, 250), Qt.LeftButton,
                   Qt.NoButton)
            before = _stamp(w.view.document)
            assert before.image is not None
            left, top, right, bottom = _screen_box(w.view, before)
            _drag(w.view, QPoint(round(right), round((top + bottom) / 2)),
                  QPoint(round(right) - 60, round((top + bottom) / 2)))  # right edge, inwards
            after = _stamp(w.view.document)
            (w0, h0), (w1, h1) = _width_height(before.rect), _width_height(after.rect)
            assert w1 < w0 * 0.8 and abs(w1 / h1 - w0 / h0) < 0.02 * (w0 / h0)
            shown = _shown(w.view.document, 0)
            assert w.save()
            assert _differing_pixels(shown, _shown(w.view.document, 0)) == 0


def _placed_stamp(w, at=QPoint(300, 300)):
    w._pick_stamp(STAMPS[0])
    _mouse(w.view, QEvent.MouseButtonPress, at, Qt.LeftButton, Qt.LeftButton)
    _mouse(w.view, QEvent.MouseButtonRelease, at, Qt.LeftButton, Qt.NoButton)
    return _stamp(w.view.document)


def test_a_small_stamp_still_moves():
    # Zoomed out, a stamp is ~9px tall: its edges' grips must not cover it,
    # or it could be resized but never moved again.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "small.pdf")
        _pdf(path)
        with _window(tmp, path) as w:
            w.view.set_scale(0.25)
            _app.processEvents()
            stamp = _placed_stamp(w, QPoint(100, 100))
            left, top, right, bottom = _screen_box(w.view, stamp)
            assert bottom - top < 12, "the case this tests: a tiny stamp on screen"
            centre = QPoint(round((left + right) / 2), round((top + bottom) / 2))
            _drag(w.view, centre, centre + QPoint(30, 30))
            assert w.history.undo_label() == "Move Stamp", w.history.undo_label()
            moved = _stamp(w.view.document)
            (mw, mh), (sw, sh) = _width_height(moved.rect), _width_height(stamp.rect)
            assert abs(mw - sw) < 0.01 and abs(mh - sh) < 0.01


def test_dragging_past_the_far_edge_stops_at_the_smallest():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "cross.pdf")
        _pdf(path)
        with _window(tmp, path) as w:
            stamp = _placed_stamp(w)
            left, top, right, bottom = _screen_box(w.view, stamp)
            middle = round((top + bottom) / 2)
            _drag(w.view, QPoint(round(right), middle), QPoint(round(left) - 150, middle))
            after = _stamp(w.view.document)
            l2, t2, r2, b2 = _screen_box(w.view, after)
            assert abs(l2 - left) < 1, "the left edge stays put"
            assert r2 - l2 < (right - left) / 2, "smaller, not mirrored and growing"


def test_a_stamp_with_an_empty_box_doesnt_break_resizing():
    # Another app saved a stamp with a zero-height /Rect: it can't be
    # resized (there's no shape to keep), and must not raise on hover/drag.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "flat.pdf")
        pdf = pikepdf.Pdf.new()
        pdf.add_blank_page(page_size=(612, 792))
        ap = pdf.make_stream(b"0 0 1 rg 0 0 100 10 re f")
        ap.Type, ap.Subtype, ap.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 100, 10]
        stamp = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Stamp, Rect=[200, 400, 300, 400],
            F=4, AP=pikepdf.Dictionary(N=ap)))
        pdf.pages[0].Annots = pdf.make_indirect(pikepdf.Array([stamp]))
        pdf.save(path)
        with _window(tmp, path) as w:
            flat = _stamp(w.view.document)
            left, top, right, bottom = _screen_box(w.view, flat)
            assert w.view._resize_hit(w.view._pages[0], QPoint(round(right), round(bottom))) is None
            _drag(w.view, QPoint(round(right), round(bottom)),
                  QPoint(round(right) + 40, round(bottom) + 40))  # no exception
            assert w.history is None or w.history.undo_label() != "Resize Stamp"


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
