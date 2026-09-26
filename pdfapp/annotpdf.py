"""Annotation edits -> PDF objects, with pikepdf.

apply_edits() is the single place annotation edits become PDF. The writer
runs it on the real pages at save time; overlay_pdf() runs it on a
throwaway one-page document that pdfium renders over the page for the live
preview. Same function both ways, so what's on screen is what gets saved.

Every annotation created here gets its own appearance stream (/AP). Viewers
are allowed to draw an annotation without one however they like, and do --
Acrobat, Edge and Chrome would each show a different note icon.
"""

from __future__ import annotations

import io
import time
import uuid
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from .annots import MARKUP_DEFAULT_COLORS, NOTE_SIZE, Annot, is_editable


def pdf_date(t: Optional[float] = None) -> str:
    """PDF date string, local time with UTC offset: D:YYYYMMDDHHmmSS+HH'mm'."""
    t = time.time() if t is None else t
    lt = time.localtime(t)
    offset = lt.tm_gmtoff // 60
    sign = "+" if offset >= 0 else "-"
    return (time.strftime("D:%Y%m%d%H%M%S", lt)
            + f"{sign}{abs(offset) // 60:02d}'{abs(offset) % 60:02d}'")


def _rgb(color) -> str:
    return " ".join(f"{max(0.0, min(1.0, c)):.3f}" for c in color)


def _note_appearance(pdf: pikepdf.Pdf, color) -> pikepdf.Stream:
    """A speech bubble with three lines of 'text', NOTE_SIZE points square."""
    s = NOTE_SIZE
    edge = tuple(c * 0.45 for c in color)
    ops = (
        f"q 1 w {_rgb(edge)} RG {_rgb(color)} rg "
        # bubble body with a tail at bottom-left
        f"1.5 {s * 0.28:.2f} m {s - 1.5:.2f} {s * 0.28:.2f} l {s - 1.5:.2f} {s - 1.5:.2f} l "
        f"1.5 {s - 1.5:.2f} l h B "
        f"{s * 0.22:.2f} {s * 0.28:.2f} m {s * 0.22:.2f} 1 l {s * 0.48:.2f} {s * 0.28:.2f} l h B "
        f"{_rgb(edge)} RG 1.2 w "
        + "".join(f"{s * 0.2:.2f} {y:.2f} m {s * 0.8:.2f} {y:.2f} l S "
                  for y in (s * 0.78, s * 0.62, s * 0.46))
        + "Q"
    )
    stream = pdf.make_stream(ops.encode("ascii"))
    stream.Type = Name.XObject
    stream.Subtype = Name.Form
    stream.BBox = Array([0, 0, s, s])
    stream.Resources = Dictionary()
    return stream


def _quads(annot: Annot):
    q = annot.quads
    for i in range(0, len(q) - 7, 8):
        # upper-left, upper-right, lower-left, lower-right
        yield (q[i], q[i + 1]), (q[i + 2], q[i + 3]), (q[i + 4], q[i + 5]), (q[i + 6], q[i + 7])


def _markup_appearance(pdf: pikepdf.Pdf, annot: Annot) -> pikepdf.Stream:
    """Drawn in page coordinates: BBox == Rect with an identity Matrix maps
    the form straight onto the page, so quads need no conversion."""
    ops = ["q"]
    resources = Dictionary()
    if annot.kind == "Highlight":
        # Multiply: the colour darkens the page under it instead of covering
        # it, so black text stays black. What Acrobat does.
        resources.ExtGState = Dictionary(GS0=Dictionary(Type=Name.ExtGState, BM=Name.Multiply))
        ops.append(f"/GS0 gs {_rgb(annot.color)} rg")
        for ul, ur, ll, lr in _quads(annot):
            ops.append(f"{ll[0]:.3f} {ll[1]:.3f} m {lr[0]:.3f} {lr[1]:.3f} l "
                       f"{ur[0]:.3f} {ur[1]:.3f} l {ul[0]:.3f} {ul[1]:.3f} l h f")
    else:
        ops.append(f"{_rgb(annot.color)} RG")
        for ul, ur, ll, lr in _quads(annot):
            height = abs(ul[1] - ll[1])
            width = max(0.6, height * 0.07)
            # Underline sits just above the bottom of the line box (which
            # includes descenders); strikeout goes through the x-height.
            t = 0.12 if annot.kind == "Underline" else 0.4
            y0 = ll[1] + (ul[1] - ll[1]) * t
            y1 = lr[1] + (ur[1] - lr[1]) * t
            ops.append(f"{width:.3f} w {ll[0]:.3f} {y0:.3f} m {lr[0]:.3f} {y1:.3f} l S")
    ops.append("Q")
    stream = pdf.make_stream(" ".join(ops).encode("ascii"))
    stream.Type = Name.XObject
    stream.Subtype = Name.Form
    stream.BBox = Array(list(annot.rect))
    stream.Resources = resources
    return stream


def build_annot(pdf: pikepdf.Pdf, annot: Annot) -> pikepdf.Object:
    """A new annotation dictionary (indirect) for an Annot created here."""
    now = pdf_date()
    if annot.kind in ("Highlight", "Underline", "StrikeOut"):
        d = Dictionary(
            Type=Name.Annot, Subtype=Name("/" + annot.kind),
            Rect=Array(list(annot.rect)),
            QuadPoints=Array(list(annot.quads)),
            Contents=String(annot.contents),
            T=String(annot.author),
            C=Array(list(annot.color)),
            NM=String(f"butterpdf-{uuid.uuid4()}"),
            M=String(now), CreationDate=String(now),
            F=4,  # Print
        )
        d.AP = Dictionary(N=_markup_appearance(pdf, annot))
        return pdf.make_indirect(d)
    if annot.kind == "Text":
        d = Dictionary(
            Type=Name.Annot, Subtype=Name.Text,
            Rect=Array(list(annot.rect)),
            Contents=String(annot.contents),
            T=String(annot.author),
            C=Array(list(annot.color)),
            Name=Name.Comment,
            NM=String(f"butterpdf-{uuid.uuid4()}"),
            M=String(now), CreationDate=String(now),
            F=4,  # Print
            Open=False,
        )
        d.AP = Dictionary(N=_note_appearance(pdf, annot.color))
        return pdf.make_indirect(d)
    raise ValueError(f"can't create {annot.kind} annotations yet")


def _shift_numbers(arr, dx: float, dy: float) -> Array:
    """Shift a flat [x y x y ...] array."""
    return Array([float(v) + (dx if i % 2 == 0 else dy) for i, v in enumerate(arr)])


def translate(d: Dictionary, dx: float, dy: float) -> None:
    """Move an existing annotation, geometry and all.

    /Rect alone moves what pdfium and Acrobat draw from the appearance
    stream, but a viewer that regenerates the appearance from the geometry
    keys would snap it back. So those move too.
    """
    if "/Rect" in d:
        d.Rect = _shift_numbers(d.Rect, dx, dy)
    for key in ("/QuadPoints", "/Vertices", "/L", "/CL"):
        if isinstance(d.get(key), Array):
            d[key] = _shift_numbers(d[key], dx, dy)
    if isinstance(d.get("/InkList"), Array):
        d.InkList = Array([_shift_numbers(path, dx, dy) for path in d.InkList])
    popup = d.get("/Popup")
    if isinstance(popup, Dictionary) and "/Rect" in popup:
        popup.Rect = _shift_numbers(popup.Rect, dx, dy)


def _kind(d) -> str:
    return str(d.get("/Subtype", "/"))[1:]


def _flags(d) -> int:
    try:
        return int(d.get("/F", 0))
    except (TypeError, ValueError):
        return 0


def _reshape_markup(pdf: pikepdf.Pdf, d: Dictionary, a: Annot) -> None:
    """New geometry for an existing markup cut back by a newer one. Its old
    appearance showed the old shape, so it gets a new one, in its own colour."""
    try:
        color = tuple(float(c) for c in d.C)
        if len(color) != 3:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        color = MARKUP_DEFAULT_COLORS.get(a.kind, (1.0, 1.0, 0.0))
    d.QuadPoints = Array(list(a.quads))
    d.Rect = Array(list(a.rect))
    d.AP = Dictionary(N=_markup_appearance(pdf, replace(a, color=color)))


def apply_edits(pdf: pikepdf.Pdf, page_obj: Dictionary, annots: Sequence[Annot]) -> None:
    """Make page_obj's /Annots match `annots`.

    `annots` is the complete editable set for the page (see annots.py):
    existing entries are matched by their index in the page's CURRENT
    /Annots, so this must run before anything reorders or prunes that array.
    Editable annotations absent from `annots` are deleted, along with their
    popups and any replies to them. Everything else passes through, in order.
    """
    original = list(page_obj.get("/Annots") or [])
    by_origin = {a.origin: a for a in annots if a.origin is not None}

    deleted = set()
    for i, d in enumerate(original):
        if isinstance(d, Dictionary) and is_editable(_kind(d), _flags(d)) \
                and i not in by_origin and d.is_indirect:
            deleted.add(d.objgen)
    # Popups of deleted annotations, and replies to them (/IRT), to any depth.
    changed = True
    while changed:
        changed = False
        for d in original:
            if not isinstance(d, Dictionary) or not d.is_indirect or d.objgen in deleted:
                continue
            for key in ("/Parent", "/IRT"):
                ref = d.get(key)
                if isinstance(ref, Dictionary) and ref.is_indirect and ref.objgen in deleted \
                        and (key == "/IRT" or _kind(d) == "Popup"):
                    deleted.add(d.objgen)
                    changed = True

    result: List[pikepdf.Object] = []
    now = None
    for i, d in enumerate(original):
        if isinstance(d, Dictionary) and d.is_indirect and d.objgen in deleted:
            continue
        a = by_origin.get(i)
        if a is not None and isinstance(d, Dictionary):
            if a.offset != (0.0, 0.0):
                translate(d, *a.offset)
            if a.edited:
                d.Contents = String(a.contents)
            if a.reshaped:
                _reshape_markup(pdf, d, a)
            if a.offset != (0.0, 0.0) or a.edited or a.reshaped:
                now = now or pdf_date()
                d.M = String(now)
        result.append(d)
    for a in annots:
        if a.origin is None:
            new = build_annot(pdf, a)
            new.P = page_obj
            result.append(new)

    if result:
        page_obj.Annots = Array(result)
    elif "/Annots" in page_obj:
        del page_obj["/Annots"]


def overlay_pdf(
    source: Path,
    password: Optional[str],
    page_index: int,
    annots: Sequence[Annot],
    mediabox: Tuple[float, float, float, float],
    cropbox: Tuple[float, float, float, float],
    rotate: int,
) -> bytes:
    """A one-page PDF holding only this page's annotations, edits applied.

    Same boxes and /Rotate as the real page, so pdfium maps it onto exactly
    the same pixels; the page itself is left empty. Rendered over the page's
    content (drawn without annotations), it shows precisely what saving
    would produce.

    The source is opened, copied from and closed here -- nothing is saved
    back, so it's fine that its annotations get their /P and /Parent links
    cut: those would otherwise drag the whole source page, and the form
    field tree, into the copy.
    """
    with pikepdf.open(source, password=password or "") as src:
        out = pikepdf.Pdf.new()
        out.add_blank_page()  # boxes replaced just below
        page = out.pages[0].obj
        page.MediaBox = Array(list(mediabox))
        page.CropBox = Array(list(cropbox))
        if rotate:
            page.Rotate = rotate

        src_annots = src.pages[page_index].obj.get("/Annots")
        if isinstance(src_annots, Array):
            for d in src_annots:
                if isinstance(d, Dictionary):
                    for key in ("/P", "/Parent") if _kind(d) != "Popup" else ("/P",):
                        if key in d:
                            del d[key]
            # One copy_foreign for the whole array keeps references between
            # its entries (popup <-> parent, replies) pointing at the copies.
            # It only takes indirect objects, and /Annots is often direct.
            if not src_annots.is_indirect:
                src_annots = src.make_indirect(src_annots)
            page.Annots = out.copy_foreign(src_annots)
        apply_edits(out, page, annots)

        buf = io.BytesIO()
        with warnings.catch_warnings():
            # Widgets without an /AcroForm: deliberate here, pdfium only needs
            # their appearance streams, and this document is never saved.
            warnings.simplefilter("ignore", pikepdf.PageCopyWarning)
            out.save(buf)
        return buf.getvalue()
