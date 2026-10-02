"""Signatures: picture signatures (an image stamp, no certificate), digital
IDs (self-made .pfx, and Windows certificates through CNG), certificate
signing with our own appearance, and checking signatures.

Run with pytest, or directly:  python tests/test_signatures.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pikepdf  # noqa: E402
from pikepdf import Name  # noqa: E402
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pdfapp.annots import new_signature_image, signature_click_box  # noqa: E402
from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.sigimage import SignatureImageError, from_qimage  # noqa: E402
from pdfapp.writer import commit, write_pdf  # noqa: E402
from test_annots import _differing_pixels, _make_annotated_pdf, _overlay, _render, _shown  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _scanned_signature(w=600, h=220) -> QImage:
    """Dark-blue ink on off-white paper, like a phone photo of a signature."""
    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(QColor(246, 244, 240))
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(20, 30, 110), 7, Qt.SolidLine, Qt.RoundCap))
    pts = [QPointF(80 + i * 22, 110 + (35 if i % 2 else -35)) for i in range(20)]
    for a, b in zip(pts, pts[1:]):
        p.drawLine(a, b)
    p.end()
    return image


def _plane(image, which):
    return zlib.decompress(image.rgb if which == "rgb" else image.alpha)


# ---- picture signatures ----------------------------------------------------

def test_paper_becomes_transparent_and_is_trimmed():
    sig = from_qimage(_scanned_signature())
    alpha = _plane(sig, "alpha")
    assert len(alpha) == sig.width * sig.height
    assert sig.width < 600 and sig.height < 220, "trimmed to the ink"
    assert alpha.count(0) > len(alpha) * 0.5, "the paper is see-through"
    assert max(alpha) == 255, "the ink stays solid"
    rgb = _plane(sig, "rgb")
    inky = [i for i, a in enumerate(alpha) if a == 255]
    r, g, b = rgb[inky[0] * 3:inky[0] * 3 + 3]
    assert b > r and b > g, "ink keeps its colour"
    blank = QImage(100, 50, QImage.Format_RGB32)
    blank.fill(QColor(255, 255, 255))
    try:
        from_qimage(blank)
    except SignatureImageError:
        pass
    else:
        raise AssertionError("a blank picture should be refused")


def test_signature_box_keeps_the_aspect():
    sig = from_qimage(_scanned_signature())
    stamp = new_signature_image(sig, (100, 100, 400, 400), "t", 0)  # a square box
    x0, y0, x1, y1 = stamp.rect
    assert abs((x1 - x0) / (y1 - y0) - sig.aspect) < 0.01 and abs(x1 - x0 - 300) < 0.01
    turned = new_signature_image(sig, signature_click_box(sig, 300, 400, 90), "t", 90)
    x0, y0, x1, y1 = turned.rect
    assert abs((y1 - y0) - 144) < 0.01, "2 inches wide as read, on a page shown turned"


def test_picture_signature_preview_matches_saved():
    sig = from_qimage(_scanned_signature())
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)  # page 3 is /Rotate 90
        with PdfDocument(src) as doc:
            for i in (0, 2):
                rot = doc.page_rotation(i)
                stamp = new_signature_image(sig, signature_click_box(sig, 300, 300, rot), "t", rot)
                doc.annots = doc.annots.with_page(doc.page_key(i), doc.file_annots(i) + (stamp,))
            preview = {i: _render(doc, i, 0, _overlay(doc, i)) for i in (0, 2)}
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots), out)
        with PdfDocument(out) as saved:
            for i, want in preview.items():
                bad = _differing_pixels(_shown(saved, i), want)
                assert bad == 0, f"page {i + 1}: {bad} pixels differ"
        with pikepdf.open(out) as pdf:
            stamp = [a for a in pdf.pages[0].obj.Annots if a.get("/Subtype") == Name.Stamp][-1]
            assert stamp.Name == Name("/Signature")
            image = stamp.AP.N.Resources.XObject.Im0
            assert int(image.Width) == sig.width and "/SMask" in image


def test_picture_signature_reads_upright_on_rotated_page():
    sig = from_qimage(_scanned_signature())
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            rot = doc.page_rotation(2)
            stamp = new_signature_image(sig, signature_click_box(sig, 300, 400, rot), "t", rot)
            doc.annots = doc.annots.with_page(doc.page_key(2), doc.file_annots(2) + (stamp,))
            w, h, after = _render(doc, 2, 0, _overlay(doc, 2), scale=1.0)
            xs, ys = [], []
            for i in range(0, len(after), 4):
                b, g, r = after[i], after[i + 1], after[i + 2]
                if 80 < b < 180 and r < 60 and g < 60:  # dark-blue ink, not the page's bright-blue border
                    xs.append((i // 4) % w)
                    ys.append((i // 4) // w)
            assert xs and max(xs) - min(xs) > 2 * (max(ys) - min(ys)), "signature not upright"


# ---- digital IDs and certificate signatures -----------------------------------

import datetime as dt  # noqa: E402

from pdfapp import digitalid, pdfsign  # noqa: E402

NO_TRUST = ([], [])


def _id(tmp, name="Jane Tester", password="pw-123"):
    path = Path(tmp, f"{name}.pfx")
    digitalid.create_pfx_id(path, name, password, email="jane@example.com", organization="QA")
    return path, password


def _look(name="Jane Tester", image=None, reason="I approve this document"):
    return pdfsign.Look(name=name, when=dt.datetime.now().astimezone(), reason=reason,
                        image=image)


def _plain_pdf(path, rotate=0, encryption=None):
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                BaseFont=Name.Helvetica))
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0]
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.Contents = pdf.make_stream(b"BT /F1 24 Tf 72 700 Td (Contract) Tj ET")
    if rotate:
        page.Rotate = rotate
    pdf.save(path, encryption=encryption) if encryption else pdf.save(path)


def test_pfx_id_create_and_open():
    with tempfile.TemporaryDirectory() as tmp:
        path, pw = _id(tmp)
        signer = digitalid.pfx_signer(path, pw)
        assert digitalid.common_name(signer.signing_cert) == "Jane Tester"
        info = digitalid.describe_pfx(path, pw)
        assert info.self_signed and info.expires.year >= dt.date.today().year + 4
        try:
            digitalid.pfx_signer(path, "wrong")
        except digitalid.WrongPassword:
            pass
        else:
            raise AssertionError("a wrong password opened the ID")


def test_sign_and_check():
    with tempfile.TemporaryDirectory() as tmp:
        src, signed = Path(tmp, "c.pdf"), Path(tmp, "c-signed.pdf")
        _plain_pdf(src)
        path, pw = _id(tmp)
        signer = digitalid.pfx_signer(path, pw)
        name = pdfsign.sign(src, None, signed, 0, (300, 100, 540, 180), 0,
                            _look(image=from_qimage(_scanned_signature())), signer)
        assert name == "Signature1"
        assert signed.read_bytes().startswith(src.read_bytes()), "incremental: original kept"
        (info,) = pdfsign.check(signed, trust=NO_TRUST)
        assert info.level == pdfsign.UNTRUSTED and "self-signed" in info.message, info
        assert info.signer == "Jane Tester" and info.reason == "I approve this document"
        trusted = ([digitalid.pfx_signer(path, pw).signing_cert], [])
        (info,) = pdfsign.check(signed, trust=trusted)
        assert info.level == pdfsign.OK, info
        with PdfDocument(signed) as doc:
            assert doc.signature_count() == 1
            assert doc.access().full, "signing doesn't restrict the document"


def test_appearance_shows_picture_and_text():
    with tempfile.TemporaryDirectory() as tmp:
        src, signed = Path(tmp, "c.pdf"), Path(tmp, "s.pdf")
        _plain_pdf(src)
        path, pw = _id(tmp)
        box = (300, 100, 540, 180)
        pdfsign.sign(src, None, signed, 0, box, 0,
                     _look(image=from_qimage(_scanned_signature())),
                     digitalid.pfx_signer(path, pw))
        with PdfDocument(signed) as doc:
            w, h, px = _render(doc, 0, 0, None, scale=1.0)
        def inked(x0, x1):  # dark pixels in a horizontal band of the box
            n = 0
            for y in range(792 - 180, 792 - 100):
                for x in range(x0, x1):
                    i = (y * w + x) * 4
                    if max(px[i:i + 3]) < 140:
                        n += 1
            return n
        assert inked(305, 415) > 50, "the picture is on the left half"
        assert inked(425, 535) > 50, "the text is on the right half"


def test_second_signature_keeps_the_first():
    with tempfile.TemporaryDirectory() as tmp:
        src, one, two = Path(tmp, "c.pdf"), Path(tmp, "1.pdf"), Path(tmp, "2.pdf")
        _plain_pdf(src)
        a, pa = _id(tmp, "Ann Approver")
        b, pb = _id(tmp, "Bob Reviewer")
        pdfsign.sign(src, None, one, 0, (72, 100, 280, 160), 0, _look("Ann Approver"),
                     digitalid.pfx_signer(a, pa))
        assert pdfsign.sign(one, None, two, 0, (320, 100, 540, 160), 0, _look("Bob Reviewer"),
                            digitalid.pfx_signer(b, pb)) == "Signature2"
        trust = ([digitalid.pfx_signer(a, pa).signing_cert,
                  digitalid.pfx_signer(b, pb).signing_cert], [])
        infos = pdfsign.check(two, trust=trust)
        assert [i.signer for i in infos] == ["Ann Approver", "Bob Reviewer"]
        assert all(i.level == pdfsign.OK for i in infos), infos


def test_rewriting_a_signed_file_breaks_its_signature():
    """What a normal Save does to a signed PDF (the writer rewrites it) --
    why the app warns first."""
    with tempfile.TemporaryDirectory() as tmp:
        src, signed, saved = Path(tmp, "c.pdf"), Path(tmp, "s.pdf"), Path(tmp, "re.pdf")
        _plain_pdf(src)
        path, pw = _id(tmp)
        pdfsign.sign(src, None, signed, 0, (300, 100, 540, 180), 0, _look(),
                     digitalid.pfx_signer(path, pw))
        with PdfDocument(signed) as doc:
            commit(write_pdf(doc.sources, doc.pages.rotated([0], 90), saved), saved)
        (info,) = pdfsign.check(saved, trust=NO_TRUST)
        assert info.level == pdfsign.BROKEN, info


def test_signature_on_a_rotated_page_reads_upright():
    with tempfile.TemporaryDirectory() as tmp:
        src, signed = Path(tmp, "r.pdf"), Path(tmp, "rs.pdf")
        _plain_pdf(src, rotate=90)
        path, pw = _id(tmp)
        # As read on screen the box is wide; in unrotated page space it's tall.
        box = (100, 300, 160, 540)
        pdfsign.sign(src, None, signed, 0, box, 90, _look(reason=""),
                     digitalid.pfx_signer(path, pw))
        with PdfDocument(signed) as doc:
            w, h, px = _render(doc, 0, 0, None, scale=1.0)
            # Only inside the signature's box as displayed: the page's own
            # heading runs sideways down the right edge of this page.
            left, top, right, bottom = doc.transform(0, 1.0).pdf_rect_to_widget(box)
        xs, ys = [], []
        for y in range(int(top), int(bottom)):
            for x in range(int(left), int(right)):
                i = (y * w + x) * 4
                if max(px[i:i + 3]) < 100:
                    xs.append(x)
                    ys.append(y)
        assert right - left > bottom - top, "the box is wide as displayed"
        assert xs and max(xs) - min(xs) > 2 * (max(ys) - min(ys)), "appearance not upright"


def test_signing_keeps_a_password():
    with tempfile.TemporaryDirectory() as tmp:
        src, signed = Path(tmp, "locked.pdf"), Path(tmp, "locked-signed.pdf")
        _plain_pdf(src, encryption=pikepdf.Encryption(user="open", owner="open"))
        path, pw = _id(tmp)
        pdfsign.sign(src, "open", signed, 0, (300, 100, 540, 180), 0, _look(),
                     digitalid.pfx_signer(path, pw))
        try:
            PdfDocument(signed).close()
        except Exception:
            pass
        else:
            raise AssertionError("signing dropped the document's password")
        (info,) = pdfsign.check(signed, "open", trust=NO_TRUST)
        assert info.level == pdfsign.UNTRUSTED, info


def _ec_pfx(tmp, password):
    """An ECDSA ID (P-256): the other key type Windows certificates use."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Eve Elliptic")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.KeyUsage(True, True, False, False, False, False, False,
                                         False, False), critical=True)
            .sign(key, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(
        b"eve", key, cert, None, serialization.BestAvailableEncryption(password.encode()))


def test_windows_cng_signing():
    """Windows-store certificates sign through CNG with the key kept in
    Windows. Tested with IDs imported into memory only (never persisted),
    RSA and ECDSA, without touching the user's certificate store."""
    if sys.platform != "win32":
        return
    from pdfapp import wincert
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "c.pdf")
        _plain_pdf(src)
        path, pw = _id(tmp, "Winnie Windows")
        for label, pfx, persist in (("rsa-memory", path.read_bytes(), False),
                                    ("ec-memory", _ec_pfx(tmp, pw), False),
                                    # The route a real store certificate takes; the key
                                    # is in the user's key store only during this signing.
                                    ("rsa-keystore", path.read_bytes(), True),
                                    ("ec-keystore", _ec_pfx(tmp, pw), True)):
            signed = Path(tmp, f"{label}.pdf")
            signer = wincert.imported_pfx_signer(pfx, pw, persist_key=persist)
            pdfsign.sign(src, None, signed, 0, (300, 100, 540, 180), 0, _look(), signer)
            (info,) = pdfsign.check(signed, trust=NO_TRUST)
            assert info.level == pdfsign.UNTRUSTED, (label, info)  # intact and valid
        ids = wincert.list_ids()  # the real store: listing must work, whatever's in it
        assert all(i.kind == digitalid.WINDOWS and i.thumbprint for i in ids)
        roots, _ = wincert.trust_certificates()
        assert len(roots) > 5, "Windows' trusted roots"


# ---- in the app ------------------------------------------------------------

from contextlib import contextmanager  # noqa: E402

from PySide6.QtCore import QEvent, QPoint, QSettings  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

from pdfapp.pageview import Tool  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402


@contextmanager
def _app_window(tmp, pdf_path, password=None):
    import pdfapp.mainwindow as mw
    ini = lambda name: QSettings(str(Path(tmp, name)), QSettings.IniFormat)  # noqa: E731
    w = mw.MainWindow(Settings(ini("user.ini"), ini("policy.ini")))
    w.resize(900, 700)
    w.show()
    try:
        w.view.set_document(PdfDocument(pdf_path, password=password))
        for _ in range(5):
            _app.processEvents()
        yield w
    finally:
        w.view.set_document(None)
        w.close()


@contextmanager
def _patched(**names):
    import pdfapp.mainwindow as mw
    saved = {k: getattr(mw, k) for k in names}
    for k, v in names.items():
        setattr(mw, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(mw, k, v)


def _fake_sign_dialog(pfx, password, picture=None):
    """Stands in for the Sign dialog: picks the given .pfx and accepts."""
    class Fake:
        def __init__(self, parent, settings, current_picture, windows_ids, hwnd):
            self.windows_ids = windows_ids
            self.picture, self.picture_path = picture, None

        def exec(self):
            from PySide6.QtWidgets import QDialog
            self.signer = digitalid.pfx_signer(pfx, password)
            self.identity = digitalid.DigitalID(digitalid.PFX, "Jane Tester", path=pfx)
            self.look = _look(image=self.picture)
            return QDialog.Accepted
    return Fake


def _drag_on_page(view, a: QPoint, b: QPoint):
    page = view._pages[0]
    ev = lambda kind, pos, button, buttons: QMouseEvent(  # noqa: E731
        kind, QPointF(pos), QPointF(page.mapToGlobal(pos)), button, buttons, Qt.NoModifier)
    view.page_mouse_press(page, ev(QEvent.MouseButtonPress, a, Qt.LeftButton, Qt.LeftButton))
    view.page_mouse_move(page, ev(QEvent.MouseMove, b, Qt.NoButton, Qt.LeftButton))
    view.page_mouse_release(page, ev(QEvent.MouseButtonRelease, b, Qt.LeftButton, Qt.NoButton))


def test_sign_with_certificate_in_the_app():
    with tempfile.TemporaryDirectory() as tmp:
        src, signed = Path(tmp, "contract.pdf"), Path(tmp, "contract-signed.pdf")
        _plain_pdf(src)
        original = src.read_bytes()
        pfx, pw = _id(tmp)
        with _app_window(tmp, src) as w, _patched(
                SignDialog=_fake_sign_dialog(pfx, pw, from_qimage(_scanned_signature())),
                choose_picture=lambda *a: None):
            w._ask_save_path = lambda title, suggested: signed
            w.act_sign_cert.trigger()  # the real menu action
            assert w.view.tool is Tool.SIGN
            _drag_on_page(w.view, QPoint(300, 600), QPoint(560, 680))
            assert w.view.tool is Tool.SELECT, "one signature per pick"
            assert w.view.document.path == signed, "the signed copy is now open"
            assert signed.read_bytes().startswith(original), "incremental: nothing rewritten"
            assert w.signature_bar.isVisible() and "Signed by Jane Tester" in w.signature_text.text()
            assert len(w._signature_infos) == 1
            assert w._signature_infos[0].level == pdfsign.UNTRUSTED, w._signature_infos

            # Sign it again, untouched: the first signature must survive.
            twice = Path(tmp, "twice.pdf")
            w._ask_save_path = lambda title, suggested: twice
            w.view.signature_box.emit(0, (72, 100, 280, 160))
            assert [i.level for i in w._signature_infos] == [pdfsign.UNTRUSTED] * 2
            assert "2 signatures" in w.signature_text.text()


def test_saving_a_signed_document_warns_and_save_as_keeps_it_valid():
    with tempfile.TemporaryDirectory() as tmp:
        src, signed = Path(tmp, "c.pdf"), Path(tmp, "signed.pdf")
        _plain_pdf(src)
        pfx, pw = _id(tmp)
        pdfsign.sign(src, None, signed, 0, (300, 100, 540, 180), 0, _look(),
                     digitalid.pfx_signer(pfx, pw))
        warned = []

        class Box:
            Save, Cancel = 1, 0

            @staticmethod
            def warning(parent, title, text, *a):
                warned.append(text)
                return Box.Cancel
        with _app_window(tmp, signed) as w, _patched(QMessageBox=Box):
            # Untouched Save As: a byte copy, still validly signed.
            copy = Path(tmp, "copy.pdf")
            w._ask_save_path = lambda title, suggested: copy
            assert w.save_as()
            assert copy.read_bytes() == signed.read_bytes() and not warned
            assert w._signature_infos[0].level == pdfsign.UNTRUSTED, "still intact"
            # An edit, then Save: warned first, and Cancel keeps the file as it was.
            w.rotate_pages([0], 90)
            before = copy.read_bytes()
            assert not w.save()
            assert warned and "signature" in warned[0] and copy.read_bytes() == before


def test_picture_signature_tool_in_the_app():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "c.pdf")
        _plain_pdf(src)
        picture = Path(tmp, "my-signature.png")
        _scanned_signature().save(str(picture))
        with _app_window(tmp, src) as w:
            w.settings.set_signature_picture(str(picture))
            w.act_sign_picture.trigger()
            assert w.view.tool is Tool.SIGNATURE, "the remembered picture is used"
            _drag_on_page(w.view, QPoint(200, 500), QPoint(420, 580))
            (stamp,) = [a for a in w.view.document.page_annots(0) if a.image is not None]
            assert w.history.undo_label() == "Add Signature" and w.view.tool is Tool.SELECT


def test_restricted_document_blocks_signing():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "locked.pdf")
        _plain_pdf(src, encryption=pikepdf.Encryption(
            user="", owner="own", allow=pikepdf.Permissions(modify_annotation=False,
                                                            modify_other=False,
                                                            modify_form=False)))
        with _app_window(tmp, src) as w:
            assert not w.act_sign_cert.isEnabled() and not w.act_sign_picture.isEnabled()
            w.view.signature_box.emit(0, (100, 100, 300, 160))  # a request no button stops
            assert w.view.document.path == src and not w.history.can_undo()


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
