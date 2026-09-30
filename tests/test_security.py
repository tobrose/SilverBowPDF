"""Encryption: saving keeps a document's protection; File > Password
Protection sets, changes or removes an open password; an author's
restrictions are honoured until unlocked with the owner password.

UI parts run on the offscreen platform with INI settings -- never the real
registry.

Run with pytest, or directly:  python tests/test_security.py
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

from pdfapp.document import PasswordRequired, PdfDocument  # noqa: E402
from pdfapp.protection import REMOVE_CHOICE, SET  # noqa: E402
from pdfapp.security import access_from_bits  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402

_app = QApplication.instance() or QApplication([])

LOCKED_DOWN = pikepdf.Permissions(extract=False, print_highres=False, print_lowres=False,
                                  modify_other=False, modify_annotation=False,
                                  modify_form=False, modify_assembly=False)


def _make(path, encryption=None, pages=2):
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    for i in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        page.Contents = pdf.make_stream(
            f"BT /F1 18 Tf 72 700 Td (Secret page {i + 1}) Tj ET".encode())
    pdf.save(path, encryption=encryption) if encryption else pdf.save(path)


def _opens(path, password=None) -> bool:
    try:
        PdfDocument(path, password=password).close()
        return True
    except PasswordRequired:
        return False


class _Window:
    def __init__(self, tmp, path, password=None):
        from pdfapp.mainwindow import MainWindow
        ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
        self.w = MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
        self.w.resize(900, 700)
        self.w.show()
        self.w.view.set_document(PdfDocument(path, password=password))
        _app.processEvents()

    def __enter__(self):
        return self.w

    def __exit__(self, *exc):
        self.w.view.set_document(None)  # drops history: no save prompt
        self.w.close()


def test_access_from_permission_bits():
    assert access_from_bits(-1, 0).full and not access_from_bits(-1, 0).encrypted
    locked = access_from_bits(6, 0xFFFFF2C0)  # measured: LOCKED_DOWN, no owner password
    assert locked.encrypted and not any((locked.print, locked.copy, locked.annotate,
                                         locked.fill_forms, locked.modify, locked.assemble))
    assert "editing" in locked.restricted() and "printing" in locked.restricted()
    some = access_from_bits(4, 0xFFFFFBE4)  # measured: no copying, no modifying
    assert some.print and some.annotate and some.fill_forms
    assert not some.copy and not some.modify and not some.assemble
    assert not some.full
    assert access_from_bits(6, 0xFFFFFFFC).full, "the owner's"


def test_document_reports_access_per_password():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "locked.pdf")
        _make(path, pikepdf.Encryption(user="", owner="own", allow=LOCKED_DOWN))
        with PdfDocument(path) as doc:  # opens for anyone...
            a = doc.access()
            assert a.encrypted and not a.annotate and not a.print, "...restricted"
        with PdfDocument(path, password="own") as doc:
            assert doc.access().full


def test_restrictions_are_honoured_until_unlocked():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "locked.pdf")
        _make(path, pikepdf.Encryption(user="", owner="own", allow=LOCKED_DOWN))
        with _Window(tmp, path) as w:
            assert w.restriction_bar.isVisible()
            assert "editing" in w.restriction_text.text()
            assert not w.annotate_button.isEnabled() and not w.form_button.isEnabled()
            for act in (w.act_print, w.act_page_rot_cw, w.act_page_delete, w.act_ocr,
                        w.act_page_extract):
                assert not act.isEnabled(), act.text()
            assert w.act_protect.isEnabled(), "shows what's set, and offers Unlock"

            w.rotate_pages([0], 90)  # a request no disabled button can stop
            assert not w.history.can_undo(), "refused"
            assert "doesn't allow" in w.statusBar().currentMessage()
            w.set_protection(SET, "mine")
            assert w.history.current.protection is None, "can't re-protect it your own way"
            assert not w.view.access.annotate, "the page view knows too"

            w._on_editing_changed(False)  # re-enables tools... except restricted ones
            assert not w.act_tool_highlight.isEnabled()

            assert w.unlock("own")
            _app.processEvents()
            assert not w.restriction_bar.isVisible() and w.annotate_button.isEnabled()
            assert w.act_print.isEnabled() and w.view.access.annotate
            w.rotate_pages([0], 90)
            assert w.history.undo_label() == "Rotate Page 1 Right"


def test_unlock_keeps_edits_and_history():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "some.pdf")
        # Filling forms and commenting allowed, page changes not.
        _make(path, pikepdf.Encryption(user="", owner="own",
                                       allow=pikepdf.Permissions(modify_assembly=False,
                                                                 modify_other=False)))
        with _Window(tmp, path) as w:
            key = w.view.document.page_key(0)
            from pdfapp.annots import new_note
            w._on_annots_edit(key, (new_note(100, 700, "hi", "t"),), "Add Note")
            assert w.history.can_undo(), "commenting is allowed"
            w.unlock("own")
            assert w.history.undo_label() == "Add Note", "history carried over"
            assert len(w.view.document.page_annots(0)) == 1


def test_save_keeps_the_password():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "secret.pdf")
        _make(path, pikepdf.Encryption(user="pw", owner="pw"))
        with _Window(tmp, path, "pw") as w:
            w.rotate_pages([0], 90)
            assert w.save()
            assert not w.history.dirty and w.view.document.password == "pw"
        assert not _opens(path), "still protected"
        with PdfDocument(path, password="pw") as doc:
            assert doc.page_rotation(0) == 90, "and the edit is in"


def test_add_change_remove_password():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "plain.pdf")
        _make(path)
        with _Window(tmp, path) as w:
            w.set_protection(SET, "first")
            assert w.history.undo_label() == "Add Password" and w.history.dirty
            w.undo()
            assert w.history.current.protection is None, "undoable"
            w.redo()
            assert w.save()
            assert w.view.document.is_encrypted and w.view.document.password == "first"
        assert not _opens(path) and _opens(path, "first")

        with _Window(tmp, path, "first") as w:
            w.set_protection(SET, "second")
            assert w.history.undo_label() == "Change Password"
            assert w.save()
        assert not _opens(path, "first") and _opens(path, "second")

        with _Window(tmp, path, "second") as w:
            w.set_protection(REMOVE_CHOICE)
            assert w.history.undo_label() == "Remove Password"
            assert w.save()
            assert not w.view.document.is_encrypted
        assert _opens(path), "anyone can open it now"


def test_protection_dialog_states():
    from pdfapp.protection import ProtectionDialog
    from PySide6.QtWidgets import QPushButton
    labels = lambda d: {b.text() for b in d.findChildren(QPushButton)}  # noqa: E731
    plain = ProtectionDialog(None, "a.pdf", False, False, None, True)
    assert "Set Password" in labels(plain) and "Remove Password" not in labels(plain)
    protected = ProtectionDialog(None, "a.pdf", True, True, None, True)
    assert {"Change Password", "Remove Password"} <= labels(protected)
    locked = ProtectionDialog(None, "a.pdf", True, False, None, False)
    assert "Unlock…" in labels(locked) and "Set Password" not in labels(locked)
    plain.first.setText("a")
    plain.second.setText("b")
    plain._set()
    assert plain.choice is None and "different" in plain.error.text()
    plain.second.setText("a")
    plain._set()
    assert plain.choice == (SET, "a")


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
