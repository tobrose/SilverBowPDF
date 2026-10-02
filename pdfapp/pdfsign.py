"""Certificate signatures: add one, and check the ones a file has. No Qt.

Signing is an INCREMENTAL update (pyHanko): the signed file is the
original bytes plus an appended revision holding the new signature field
and the signature. Earlier signatures stay valid -- unlike a normal save,
which rewrites the whole file (writer.py, qpdf) and breaks every one.

The appearance is ours, not pyHanko's: drawn here -- the signature picture,
"Digitally signed by", the name, date and reason -- counter-rotated by the
page's rotation so it reads upright on a turned page (pyHanko's stamps
don't), and set on the widget before signing; pyHanko then signs with its
NoOp stamp style, which keeps it.

Checking uses Windows' trusted roots (wincert.trust_certificates): a
certificate the company issued checks out as trusted, as in Outlook.
Revocation isn't checked (no network fetching): a desktop opening a
PDF shouldn't stall on an OCSP server.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .annots import SignatureImage
from .textlayout import encodable, text_width

OK, UNTRUSTED, CHANGED, BROKEN = "ok", "untrusted", "changed", "broken"
_COUNTER_ROTATION = {0: [1, 0, 0, 1, 0, 0], 90: [0, 1, -1, 0, 0, 0],
                     180: [-1, 0, 0, -1, 0, 0], 270: [0, -1, 1, 0, 0, 0]}
REASONS = ("I am the author of this document", "I approve this document",
           "I have reviewed this document", "I agree to the terms defined in this document")


class SigningError(Exception):
    pass


@dataclass(frozen=True)
class Look:
    """What the signature shows."""
    name: str
    when: dt.datetime
    reason: str = ""
    location: str = ""
    image: Optional[SignatureImage] = None
    show_date: bool = True


@dataclass(frozen=True)
class SigInfo:
    """One signature's check, in words a user acts on."""
    field: str
    signer: str
    when: Optional[dt.datetime]
    level: str          # OK / UNTRUSTED / CHANGED / BROKEN
    message: str
    reason: str = ""
    issuer: str = ""


# ---- signing -------------------------------------------------------------

def _lines(look: Look) -> List[str]:
    """The appearance's text; a line that can't be drawn in Helvetica
    (WinAnsi) is left out rather than shown as '?' -- the certificate still
    carries the real name, and checking shows it."""
    lines = ["Digitally signed by"]
    if look.name and encodable(look.name):
        lines.append(look.name)
    else:
        lines = ["Digitally signed"]
    if look.show_date:
        lines.append("Date: " + look.when.strftime("%Y.%m.%d %H:%M:%S %z"))
    for label, value in (("Reason", look.reason), ("Location", look.location)):
        if value and encodable(value):
            lines.append(f"{label}: {value}")
    return lines


def _fit_text(lines: List[str], width: float, height: float) -> float:
    size = min(10.0, height / (len(lines) * 1.2))
    for line in lines:
        w = text_width(line, size)
        if w > width:
            size *= width / w
    return max(size, 3.0)


def appearance_ops(look: Look, w: float, h: float) -> Tuple[bytes, bool]:
    """Content stream for a w x h box (as read): picture on the left (if
    any), text on the right. Returns (ops, uses_image). Separate from the
    PDF objects so it's testable on its own."""
    pad = max(2.0, min(w, h) * 0.04)
    ops = []
    text_x = pad
    if look.image is not None:
        area_w, area_h = (w * 0.5 - 2 * pad, h - 2 * pad)
        iw = min(area_w, area_h * look.image.aspect)
        ih = iw / look.image.aspect
        ix, iy = pad + (area_w - iw) / 2, pad + (area_h - ih) / 2
        ops.append(f"q {iw:.3f} 0 0 {ih:.3f} {ix:.3f} {iy:.3f} cm /Im0 Do Q")
        text_x = w * 0.5 + pad
    lines = _lines(look)
    size = _fit_text(lines, w - text_x - pad, h - 2 * pad)
    lead = size * 1.2
    y = h - pad - size * 0.9 - max(0.0, (h - 2 * pad - lead * len(lines)) / 2)
    out = [op.encode("ascii") for op in ops]
    out.append(f"BT /Helv {size:.2f} Tf 0 g".encode("ascii"))
    for line in lines:
        literal = (line.encode("cp1252")  # _lines kept only what WinAnsi can show
                   .replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)"))
        out.append(f"1 0 0 1 {text_x:.3f} {y:.3f} Tm (".encode("ascii") + literal + b") Tj")
        y -= lead
    out.append(b"ET")
    return b"\n".join(out), look.image is not None


def _appearance_stream(writer, look: Look, rect, rotation: int):
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.generic import pdf_name

    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    if rotation % 180:
        w, h = h, w
    ops, with_image = appearance_ops(look, w, h)
    font = generic.DictionaryObject({
        pdf_name("/Type"): pdf_name("/Font"), pdf_name("/Subtype"): pdf_name("/Type1"),
        pdf_name("/BaseFont"): pdf_name("/Helvetica"),
        pdf_name("/Encoding"): pdf_name("/WinAnsiEncoding")})
    resources = generic.DictionaryObject({
        pdf_name("/Font"): generic.DictionaryObject({pdf_name("/Helv"): font})})
    if with_image:
        image = look.image

        def plane(data: bytes, space: str, extra=None):
            d = {pdf_name("/Type"): pdf_name("/XObject"), pdf_name("/Subtype"): pdf_name("/Image"),
                 pdf_name("/Width"): generic.NumberObject(image.width),
                 pdf_name("/Height"): generic.NumberObject(image.height),
                 pdf_name("/ColorSpace"): pdf_name(space),
                 pdf_name("/BitsPerComponent"): generic.NumberObject(8),
                 pdf_name("/Filter"): pdf_name("/FlateDecode")}
            d.update(extra or {})
            return writer.add_object(generic.StreamObject(d, encoded_data=data))
        mask = plane(image.alpha, "/DeviceGray")
        picture = plane(image.rgb, "/DeviceRGB", {pdf_name("/SMask"): mask})
        resources[pdf_name("/XObject")] = generic.DictionaryObject({pdf_name("/Im0"): picture})
    form = generic.StreamObject({
        pdf_name("/Type"): pdf_name("/XObject"), pdf_name("/Subtype"): pdf_name("/Form"),
        pdf_name("/BBox"): generic.ArrayObject([generic.FloatObject(v) for v in (0, 0, w, h)]),
        pdf_name("/Matrix"): generic.ArrayObject(
            [generic.NumberObject(v) for v in _COUNTER_ROTATION[rotation % 360]]),
        pdf_name("/Resources"): resources}, stream_data=ops)
    return writer.add_object(form)


def sign(source: Path, password: Optional[str], dest: Path, page_index: int, rect,
         rotation: int, look: Look, signer) -> str:
    """Write `dest`: `source` plus a revision with a new visible signature
    on page `page_index` in `rect` (PDF user space), its appearance reading
    upright at `rotation` (the page's /Rotate). Returns the field's name.
    `source` is read, never changed."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    from pyhanko.stamp import NoOpStampStyle

    x0, y0, x1, y1 = rect
    box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    with open(source, "rb") as src:
        writer = IncrementalPdfFileWriter(src)
        if writer.prev.encrypted:
            result = writer.encrypt(password or "")
            if getattr(result, "status", None) is not None and "FAILED" in str(result.status):
                raise SigningError("The document's password didn't open it for signing.")
        taken = {name for name, _, _ in fields.enumerate_sig_fields(writer)}
        n = 1
        while f"Signature{n}" in taken:
            n += 1
        name = f"Signature{n}"
        fields.append_signature_field(writer, fields.SigFieldSpec(
            sig_field_name=name, on_page=page_index, box=box))
        (_, _, ref), = fields.enumerate_sig_fields(writer, with_name=name)
        field = ref.get_object()
        widget = field if "/Rect" in field else field["/Kids"][0].get_object()
        from pyhanko.pdf_utils.generic import DictionaryObject, pdf_name
        widget[pdf_name("/AP")] = DictionaryObject(
            {pdf_name("/N"): _appearance_stream(writer, look, box, rotation)})
        meta = signers.PdfSignatureMetadata(
            field_name=name, reason=look.reason or None, location=look.location or None,
            md_algorithm="sha256", subfilter=fields.SigSeedSubFilter.PADES)
        pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=NoOpStampStyle())
        try:
            with open(dest, "wb") as out:
                pdf_signer.sign_pdf(writer, output=out)
        except Exception:
            Path(dest).unlink(missing_ok=True)
            raise
        finally:
            close = getattr(signer, "close", None)
            if close is not None:
                close()
    return name


# ---- checking ------------------------------------------------------------

def check(path: Path, password: Optional[str] = None,
          trust: Optional[Tuple[list, list]] = None) -> List[SigInfo]:
    """Every signature in the file, checked. `trust` is (roots, other
    certificates); default: Windows' (wincert.trust_certificates)."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.diff_analysis import ModificationLevel
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko.sign.validation.settings import KeyUsageConstraints
    from pyhanko.sign.validation.status import SignatureCoverageLevel
    from pyhanko_certvalidator import ValidationContext

    if trust is None:
        from .wincert import trust_certificates
        trust = trust_certificates()
    roots, others = trust
    # Any signing key usage will do: company certificates often carry
    # "digital signature" but not "non-repudiation", pyHanko's default.
    usage = KeyUsageConstraints(key_usage={"digital_signature", "non_repudiation"},
                                match_all_key_usages=False)
    results = []
    with open(path, "rb") as f:
        reader = PdfFileReader(f)
        if reader.encrypted:
            reader.decrypt(password or "")
        for emb in reader.embedded_signatures:
            name = emb.field_name or "Signature"
            try:
                context = ValidationContext(trust_roots=roots, other_certs=others,
                                            allow_fetching=False, revocation_mode="soft-fail")
                st = validate_pdf_signature(emb, signer_validation_context=context,
                                            key_usage_settings=usage)
            except Exception as exc:  # a damaged signature is a result, not a crash
                results.append(SigInfo(name, "unknown signer", None, BROKEN,
                                       f"The signature couldn't be checked: {exc}"))
                continue
            cert = st.signing_cert
            from .digitalid import common_name
            from .wincert import common_name_or
            signer = common_name(cert)
            when = st.signer_reported_dt
            if when is not None:
                when = when.astimezone()
            whole = st.coverage == SignatureCoverageLevel.ENTIRE_FILE
            later_ok = (st.modification_level is not None
                        and st.modification_level <= ModificationLevel.FORM_FILLING
                        and st.docmdp_ok is not False)
            if not (st.intact and st.valid):
                level = BROKEN
                message = ("The signature is broken: the document was changed after it was "
                           "signed, or the signature is damaged.")
            elif not whole and not later_ok:
                level = CHANGED
                message = "Valid when signed, but the document was changed afterwards."
            elif not st.trusted:
                level = UNTRUSTED
                message = ("The document hasn't changed since it was signed, but the signer's "
                           "identity isn't verified" + (" (a self-signed digital ID)."
                                                        if cert.self_signed in ("yes", "maybe")
                                                        else " (not issued by a trusted authority)."))
            else:
                level = OK
                message = "Valid: the signer is verified and the document hasn't changed since."
                if not whole:
                    message = ("Valid: the signer is verified; later changes only added "
                               "signatures or filled in forms.")
            reason = ""
            try:
                reason = str(emb.sig_object.get("/Reason", "") or "")
            except Exception:
                pass
            results.append(SigInfo(name, signer, when, level, message, reason,
                                   common_name_or(cert.issuer)))
    return results
