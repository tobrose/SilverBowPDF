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

import functools
import io
import math
import time
import uuid
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from .annots import (
    ARROW_LENGTH,
    MARKUP_DEFAULT_COLORS,
    NOTE_SIZE,
    STAMP_PADDING,
    STAMP_SIZE,
    STAMP_SUBSIZE,
    Annot,
    SignatureImage,
    is_editable,
    stamp_size,
    text_box_size,
)
from .formpdf import apply_field_edits, apply_form_edits, flatten_widgets, need_appearances
from .textlayout import ASCENT, LEADING, PADDING, bold_text_width, layout, text_width


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


# Rotating the appearance counter to the page: a page shown rotated 90 degrees
# clockwise needs its text drawn 90 degrees anticlockwise to read upright.
_COUNTER_ROTATION = {0: [1, 0, 0, 1, 0, 0], 90: [0, 1, -1, 0, 0, 0],
                     180: [-1, 0, 0, -1, 0, 0], 270: [0, -1, 1, 0, 0, 0]}


def _pdf_literal(text: str) -> bytes:
    raw = text.encode("cp1252")  # callers check textlayout.encodable first
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _freetext_appearance(pdf: pikepdf.Pdf, annot: Annot) -> pikepdf.Stream:
    """Drawn upright in its own w x h box; /Matrix turns it to match the page."""
    w, h = text_box_size(annot)
    size = annot.font_size
    lines = layout(annot.contents, size, w if annot.fixed_width else None).lines
    ops = [b"q"]
    if annot.boxed:
        ops.append(f"{_rgb(annot.color)} RG 1 w 0.5 0.5 {w - 1:.3f} {h - 1:.3f} re S".encode())
    # Clip to the box: text can never spill outside what hit-testing and
    # selection think the annotation is.
    ops.append(f"0 0 {w:.3f} {h:.3f} re W n BT /Helv {size:g} Tf {_rgb(annot.color)} rg".encode())
    y = h - PADDING - size * ASCENT
    for line in lines:
        ops.append(f"1 0 0 1 {PADDING:g} {y:.3f} Tm ".encode() + _pdf_literal(line) + b" Tj")
        y -= size * LEADING
    ops.append(b"ET Q")
    stream = pdf.make_stream(b" ".join(ops))
    stream.Type = Name.XObject
    stream.Subtype = Name.Form
    stream.BBox = Array([0, 0, w, h])
    stream.Matrix = Array(_COUNTER_ROTATION[annot.text_rotation % 360])
    stream.Resources = Dictionary(Font=Dictionary(Helv=_helvetica(pdf)))
    return stream


def _helvetica(pdf: pikepdf.Pdf) -> pikepdf.Object:
    return pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding))


def _freetext_fields(d: Dictionary, annot: Annot) -> None:
    """The keys other editors (Acrobat) read to re-edit the text natively."""
    rgb = _rgb(annot.color)
    hex_color = "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in annot.color)
    d.DA = String(f"/Helv {annot.font_size:g} Tf {rgb} rg")
    d.DS = String(f"font: Helvetica,sans-serif {annot.font_size:g}pt; color: #{hex_color}")
    d.BS = Dictionary(W=1 if annot.boxed else 0)
    d.Border = Array([0, 0, 1 if annot.boxed else 0])
    if annot.boxed:
        if "/IT" in d:
            del d["/IT"]
    else:
        d.IT = Name("/FreeTextTypeWriter")
    if "/RC" in d:
        del d["/RC"]  # rich text would override the plain text we just wrote
    d.Contents = String(annot.contents)
    d.Rect = Array(list(annot.rect))


# ---- shapes, lines, ink, stamps ------------------------------------------------

_K = 0.5523  # cubic Bezier handle length for a quarter ellipse


def _form(pdf: pikepdf.Pdf, ops: bytes, bbox, matrix=None, resources=None) -> pikepdf.Stream:
    stream = pdf.make_stream(ops)
    stream.Type = Name.XObject
    stream.Subtype = Name.Form
    stream.BBox = Array(list(bbox))
    if matrix is not None:
        stream.Matrix = Array(matrix)
    stream.Resources = resources if resources is not None else Dictionary()
    return stream


def _ellipse_path(x0: float, y0: float, x1: float, y1: float) -> str:
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    kx, ky = rx * _K, ry * _K
    return (f"{cx + rx:.3f} {cy:.3f} m "
            f"{cx + rx:.3f} {cy + ky:.3f} {cx + kx:.3f} {cy + ry:.3f} {cx:.3f} {cy + ry:.3f} c "
            f"{cx - kx:.3f} {cy + ry:.3f} {cx - rx:.3f} {cy + ky:.3f} {cx - rx:.3f} {cy:.3f} c "
            f"{cx - rx:.3f} {cy - ky:.3f} {cx - kx:.3f} {cy - ry:.3f} {cx:.3f} {cy - ry:.3f} c "
            f"{cx + kx:.3f} {cy - ry:.3f} {cx + rx:.3f} {cy - ky:.3f} {cx + rx:.3f} {cy:.3f} c h")


def _shape_appearance(pdf: pikepdf.Pdf, kind: str, rect, color, width: float,
                      fill=None) -> pikepdf.Stream:
    """Square / Circle, stroke kept inside the rect. Drawn in page space
    (BBox == Rect, identity Matrix)."""
    x0, y0, x1, y1 = rect
    inset = width / 2
    ix0, iy0, ix1, iy1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    path = (f"{ix0:.3f} {iy0:.3f} {ix1 - ix0:.3f} {iy1 - iy0:.3f} re" if kind == "Square"
            else _ellipse_path(ix0, iy0, ix1, iy1))
    paint = "B" if fill is not None else "S"
    ops = f"q {width:g} w {_rgb(color)} RG "
    if fill is not None:
        ops += f"{_rgb(fill)} rg "
    ops += f"{path} {paint} Q"
    return _form(pdf, ops.encode(), rect)


def _arrowhead(x1, y1, x2, y2, width: float) -> str:
    """An open arrowhead at (x2, y2) pointing away from (x1, y1)."""
    angle = math.atan2(y2 - y1, x2 - x1)
    length = max(6.0, width * ARROW_LENGTH)
    spread = math.radians(28)
    ax = x2 - length * math.cos(angle - spread)
    ay = y2 - length * math.sin(angle - spread)
    bx = x2 - length * math.cos(angle + spread)
    by = y2 - length * math.sin(angle + spread)
    return f"{ax:.3f} {ay:.3f} m {x2:.3f} {y2:.3f} l {bx:.3f} {by:.3f} l S"


def _line_appearance(pdf: pikepdf.Pdf, annot: Annot) -> pikepdf.Stream:
    x1, y1, x2, y2 = annot.line
    ops = (f"q {annot.width:g} w 1 J 1 j {_rgb(annot.color)} RG "
           f"{x1:.3f} {y1:.3f} m {x2:.3f} {y2:.3f} l S")
    if annot.arrow:
        ops += " " + _arrowhead(x1, y1, x2, y2, annot.width)
    return _form(pdf, (ops + " Q").encode(), annot.rect)


def _ink_path(stroke) -> str:
    """Smooth a stroke: curves through the midpoints between samples, with
    the samples as control points. Mouse input is jagged; this isn't."""
    pts = list(zip(stroke[0::2], stroke[1::2]))
    if len(pts) == 1:
        (x, y), = pts
        return f"{x:.3f} {y:.3f} m {x:.3f} {y:.3f} l"
    ops = [f"{pts[0][0]:.3f} {pts[0][1]:.3f} m"]
    for (ax, ay), (bx, by) in zip(pts[1:-1], pts[2:]):
        mx, my = (ax + bx) / 2, (ay + by) / 2
        ops.append(f"{ax:.3f} {ay:.3f} {ax:.3f} {ay:.3f} {mx:.3f} {my:.3f} c")
    ops.append(f"{pts[-1][0]:.3f} {pts[-1][1]:.3f} l")
    return " ".join(ops)


def _ink_appearance(pdf: pikepdf.Pdf, annot: Annot) -> pikepdf.Stream:
    ops = f"q {annot.width:g} w 1 J 1 j {_rgb(annot.color)} RG "
    ops += " ".join(f"{_ink_path(stroke)} S" for stroke in annot.ink)
    return _form(pdf, (ops + " Q").encode(), annot.rect)


def _stamp_appearance(pdf: pikepdf.Pdf, annot: Annot) -> pikepdf.Stream:
    """Rounded frame, bold label, optional small line; upright on the page
    as displayed (same counter-rotation as text boxes)."""
    w, h = stamp_size(annot.label, annot.sublabel)
    r = 4.0
    c = _rgb(annot.color)
    frame = (f"{r + 1:.3f} 1 m {w - r - 1:.3f} 1 l "
             f"{w - 1:.3f} 1 {w - 1:.3f} 1 {w - 1:.3f} {r + 1:.3f} c "
             f"{w - 1:.3f} {h - r - 1:.3f} l {w - 1:.3f} {h - 1:.3f} {w - 1:.3f} {h - 1:.3f} "
             f"{w - r - 1:.3f} {h - 1:.3f} c {r + 1:.3f} {h - 1:.3f} l 1 {h - 1:.3f} 1 {h - 1:.3f} "
             f"1 {h - r - 1:.3f} c 1 {r + 1:.3f} l 1 1 1 1 {r + 1:.3f} 1 c h")
    label_x = (w - bold_text_width(annot.label, STAMP_SIZE)) / 2
    label_y = h - STAMP_PADDING - STAMP_SIZE * 0.78
    ops = [f"q 2 w {c} RG {frame} S BT /HelvB {STAMP_SIZE:g} Tf {c} rg "
           f"1 0 0 1 {label_x:.3f} {label_y:.3f} Tm ".encode() + _pdf_literal(annot.label)
           + b" Tj"]
    if annot.sublabel:
        sub_x = (w - text_width(annot.sublabel, STAMP_SUBSIZE)) / 2
        sub_y = STAMP_PADDING + STAMP_SUBSIZE * 0.25
        ops.append(f"/Helv {STAMP_SUBSIZE:g} Tf 1 0 0 1 {sub_x:.3f} {sub_y:.3f} Tm ".encode()
                   + _pdf_literal(annot.sublabel) + b" Tj")
    ops.append(b"ET Q")
    fonts = Dictionary(
        HelvB=pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                           BaseFont=Name("/Helvetica-Bold"),
                                           Encoding=Name.WinAnsiEncoding)),
        Helv=_helvetica(pdf))
    return _form(pdf, b" ".join(ops), (0, 0, w, h),
                 matrix=_COUNTER_ROTATION[annot.text_rotation % 360],
                 resources=Dictionary(Font=fonts))


def image_xobject(pdf: pikepdf.Pdf, image: SignatureImage) -> pikepdf.Stream:
    """An image XObject with a soft mask, from planes that are already
    Flate-compressed: the data goes in as it is, never re-encoded."""
    def plane(data: bytes, space: Name) -> pikepdf.Stream:
        s = pikepdf.Stream(pdf, data)
        s.Type, s.Subtype = Name.XObject, Name.Image
        s.Width, s.Height, s.BitsPerComponent = image.width, image.height, 8
        s.ColorSpace, s.Filter = space, Name.FlateDecode
        return s
    picture = plane(image.rgb, Name.DeviceRGB)
    picture.SMask = plane(image.alpha, Name.DeviceGray)
    return picture


def _signature_image_appearance(pdf: pikepdf.Pdf, annot: Annot) -> pikepdf.Stream:
    """The picture, filling its box as read; upright on the page as
    displayed (the same counter-rotation as stamps and text boxes)."""
    x0, y0, x1, y1 = annot.rect
    w, h = x1 - x0, y1 - y0
    if annot.text_rotation % 180:
        w, h = h, w
    return _form(pdf, f"q {w:.3f} 0 0 {h:.3f} 0 0 cm /Im0 Do Q".encode(), (0, 0, w, h),
                 matrix=_COUNTER_ROTATION[annot.text_rotation % 360],
                 resources=Dictionary(XObject=Dictionary(Im0=image_xobject(pdf, annot.image))))


def _common(annot: Annot, subtype: str) -> Dictionary:
    now = pdf_date()
    return Dictionary(
        Type=Name.Annot, Subtype=Name("/" + subtype),
        Rect=Array(list(annot.rect)),
        Contents=String(annot.contents),
        T=String(annot.author),
        NM=String(f"silverbowpdf-{uuid.uuid4()}"),
        M=String(now), CreationDate=String(now),
        F=4,  # Print
    )


def _stroke_style(d: Dictionary, annot: Annot) -> None:
    d.C = Array(list(annot.color))
    d.BS = Dictionary(W=annot.width, S=Name.S)
    d.Border = Array([0, 0, annot.width])


def _existing_style(d: Dictionary):
    """(colour, width, fill) of a shape made elsewhere, for redrawing it."""
    def rgb(key):
        try:
            value = tuple(float(v) for v in d[key])
            return value if len(value) == 3 else None
        except (KeyError, TypeError, ValueError):
            return None
    width = 1.0
    try:
        if "/BS" in d and "/W" in d.BS:
            width = float(d.BS.W)
        elif "/Border" in d and len(d.Border) >= 3:
            width = float(d.Border[2])
    except (TypeError, ValueError):
        pass
    return rgb("/C") or (0.0, 0.0, 0.0), width, rgb("/IC")


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
            NM=String(f"silverbowpdf-{uuid.uuid4()}"),
            M=String(now), CreationDate=String(now),
            F=4,  # Print
        )
        d.AP = Dictionary(N=_markup_appearance(pdf, annot))
        return pdf.make_indirect(d)
    if annot.kind in ("Square", "Circle"):
        d = _common(annot, annot.kind)
        _stroke_style(d, annot)
        d.AP = Dictionary(N=_shape_appearance(pdf, annot.kind, annot.rect, annot.color,
                                              annot.width))
        return pdf.make_indirect(d)
    if annot.kind == "Line":
        d = _common(annot, "Line")
        _stroke_style(d, annot)
        d.L = Array(list(annot.line))
        none = Name("/None")  # Name.None_ would be spelled /None_
        d.LE = Array([none, Name.OpenArrow if annot.arrow else none])
        d.AP = Dictionary(N=_line_appearance(pdf, annot))
        return pdf.make_indirect(d)
    if annot.kind == "Ink":
        d = _common(annot, "Ink")
        _stroke_style(d, annot)
        d.InkList = Array([Array(list(stroke)) for stroke in annot.ink])
        d.AP = Dictionary(N=_ink_appearance(pdf, annot))
        return pdf.make_indirect(d)
    if annot.kind == "Stamp" and annot.image is not None:
        d = _common(annot, "Stamp")
        d.Name = Name("/" + (annot.stamp_name or "Signature"))
        d.AP = Dictionary(N=_signature_image_appearance(pdf, annot))
        return pdf.make_indirect(d)
    if annot.kind == "Stamp":
        d = _common(annot, "Stamp")
        d.Name = Name("/" + (annot.stamp_name or "Draft"))
        d.C = Array(list(annot.color))
        if annot.sublabel:
            d.Contents = String(f"{annot.label}\n{annot.sublabel}")
        d.AP = Dictionary(N=_stamp_appearance(pdf, annot))
        return pdf.make_indirect(d)
    if annot.kind == "FreeText":
        d = Dictionary(
            Type=Name.Annot, Subtype=Name.FreeText,
            T=String(annot.author),
            NM=String(f"silverbowpdf-{uuid.uuid4()}"),
            M=String(now), CreationDate=String(now),
            F=4,  # Print
        )
        _freetext_fields(d, annot)
        d.AP = Dictionary(N=_freetext_appearance(pdf, annot))
        return pdf.make_indirect(d)
    if annot.kind == "Text":
        d = Dictionary(
            Type=Name.Annot, Subtype=Name.Text,
            Rect=Array(list(annot.rect)),
            Contents=String(annot.contents),
            T=String(annot.author),
            C=Array(list(annot.color)),
            Name=Name.Comment,
            NM=String(f"silverbowpdf-{uuid.uuid4()}"),
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
            if a.reshaped and a.kind == "FreeText":
                _freetext_fields(d, a)
                d.AP = Dictionary(N=_freetext_appearance(pdf, a))
            elif a.reshaped and a.kind in ("Square", "Circle"):
                # Resized: the old appearance showed the old size. Redraw in
                # its own colour, width and fill.
                color, width, fill = _existing_style(d)
                d.Rect = Array(list(a.rect))
                if "/RD" in d:
                    del d["/RD"]
                d.AP = Dictionary(N=_shape_appearance(pdf, a.kind, a.rect, color, width, fill))
            elif a.reshaped and a.kind == "Stamp":
                # Resized: only the box changes. Viewers map the appearance's
                # bounding box onto /Rect, so the stamp -- any app's -- scales
                # with it; redrawing it would lose whatever it showed.
                d.Rect = Array(list(a.rect))
            elif a.reshaped:
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


def overlay_args(doc, index: int):
    """Arguments for overlay_pdf for displayed page `index` of a
    PdfDocument, or None when the page renders straight from the file.

    The one rule for when a page is layered: it has annotation edits, or it
    is a form page carrying other annotations (so they draw over the fields;
    see PdfDocument.render_bitmap). The renderer and the tests both use it.
    """
    edits = doc.annots.get(doc.page_key(index))
    forms = doc.page_has_forms(index)
    ref = doc.pages[index]
    form_state = doc.forms.get(doc.page_key(index))
    filled = doc.fields.for_file(ref.file)
    on_page = {w.name for w in doc.current_widgets(index)} if filled else set()
    values = tuple(sorted((n, v) for n, v in filled.items() if n in on_page)) or None
    if values is None and form_state is not None:
        values = ()  # structure changed: fields page, even with nothing filled
    forms = forms or form_state is not None
    if edits is None:
        if not (values is not None or (forms and doc.file_annots(index))):
            return None
        edits = doc.file_annots(index)
    source = doc.sources[ref.file]
    media, crop, rotate = doc.page_geometry(index)
    redraw = bool(values) and any(
        _needs_appearances(Path(s.path), s.password) for s in
        (doc.sources[f] for f in {0} | {r.file for r in doc.pages}))
    return (Path(source.path), source.password, ref.source, edits, media, crop, rotate, forms,
            values, redraw, form_state)


@functools.lru_cache(maxsize=16)
def _needs_appearances_cached(path: str, mtime_ns: int, password):
    with pikepdf.open(path, password=password or "") as pdf:
        return need_appearances(pdf)


def _needs_appearances(path: Path, password) -> bool:
    """Whether the saved document will ask viewers to redraw fields: true
    if any of its sources does (the writer's merge keeps the flag)."""
    return _needs_appearances_cached(str(path), path.stat().st_mtime_ns, password)


def _blank(out: pikepdf.Pdf, mediabox, cropbox, rotate) -> Dictionary:
    out.add_blank_page()
    page = out.pages[-1].obj
    page.MediaBox = Array(list(mediabox))
    page.CropBox = Array(list(cropbox))
    if rotate:
        page.Rotate = rotate
    return page


def overlay_pdf(
    source: Path,
    password: Optional[str],
    page_index: int,
    annots: Sequence[Annot],
    mediabox: Tuple[float, float, float, float],
    cropbox: Tuple[float, float, float, float],
    rotate: int,
    drop_widgets: bool = False,
    field_values=None,
    redraw_fields: bool = False,
    form_state=None,
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
        if field_values is not None:
            # Page 1: the page's form widgets -- structure and values applied
            # by the same apply_form_edits / apply_field_edits the writer
            # uses -- flattened to stand-alone fields with a form of their
            # own. pdfium's form engine draws it in place of the file's.
            if form_state is not None:
                apply_form_edits(src, page_index, form_state)
            apply_field_edits(src, dict(field_values))
            widgets, fields = flatten_widgets(src, src.pages[page_index].obj.get("/Annots") or [])
            form_page = _blank(out, mediabox, cropbox, rotate)
            copied = out.copy_foreign(src.make_indirect(Array([w for w in widgets if w is not None])))
            form_page.Annots = copied
            for w in copied:
                w.P = form_page
            src_form = src.Root.get("/AcroForm")
            out.Root.AcroForm = Dictionary(Fields=Array(list(copied)))
            if isinstance(src_form, Dictionary):
                for key in ("/DR", "/DA", "/Q"):
                    if key in src_form:
                        value = src_form[key]
                        if isinstance(value, (Dictionary, Array)):
                            # copy_foreign only takes indirect objects
                            value = out.copy_foreign(value if value.is_indirect
                                                     else src.make_indirect(value))
                        out.Root.AcroForm[key] = value
            if redraw_fields:
                out.Root.AcroForm.NeedAppearances = True
        page = _blank(out, mediabox, cropbox, rotate)

        src_annots = src.pages[page_index].obj.get("/Annots")
        if isinstance(src_annots, Array):
            if drop_widgets:
                # The form environment draws those (before this overlay).
                # Null, not remove: edits address /Annots by index.
                src_annots = Array([None if isinstance(d, Dictionary)
                                    and d.get("/Subtype") == Name.Widget else d
                                    for d in src_annots])
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
        if isinstance(page.get("/Annots"), Array):
            page.Annots = Array([d for d in page.Annots if d is not None])

        buf = io.BytesIO()
        with warnings.catch_warnings():
            # Widgets without an /AcroForm: deliberate here, pdfium only needs
            # their appearance streams, and this document is never saved.
            warnings.simplefilter("ignore", pikepdf.PageCopyWarning)
            out.save(buf)
        return buf.getvalue()
