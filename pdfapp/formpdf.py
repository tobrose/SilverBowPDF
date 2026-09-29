"""Form field values -> PDF, with pikepdf.

apply_field_edits() is the single place filled-in values become PDF: it sets
each field's /V and gives every widget of an edited field a matching look --
/AS for check boxes and radios (their appearances already exist), a newly
drawn appearance for text and combo fields. The writer runs it on each
source file before pages are copied (so field names still match); the
preview runs it on a throwaway copy of one page's widgets. Same code both
ways, as for annotations.

/NeedAppearances is never changed: pdfium (and Acrobat) ignore a field's
appearance and redraw it when that's set, so the preview copies the flag
the saved file will have, and both are drawn the same way.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from .fields import FLAG_COMB, FLAG_MULTILINE, FLAG_PASSWORD, auto_font_size
from .textlayout import layout, parse_da, text_width

_INHERITABLE = ("/FT", "/Ff", "/V", "/DV", "/DA", "/Q", "/Opt", "/MaxLen")
_COUNTER_ROTATION = {0: [1, 0, 0, 1, 0, 0], 90: [0, 1, -1, 0, 0, 0],  # anticlockwise
                     180: [-1, 0, 0, -1, 0, 0], 270: [0, -1, 1, 0, 0, 0]}


def _inherited(d: Dictionary, key: str):
    node, hops = d, 0
    while isinstance(node, Dictionary) and hops < 64:
        if key in node:
            return node[key]
        node, hops = node.get("/Parent"), hops + 1
    return None


def full_name(d: Dictionary) -> str:
    parts: List[str] = []
    node, hops = d, 0
    while isinstance(node, Dictionary) and hops < 64:
        if "/T" in node:
            parts.append(str(node.T))
        node, hops = node.get("/Parent"), hops + 1
    return ".".join(reversed(parts))


def _fields_by_name(pdf: pikepdf.Pdf) -> Dict[str, Dictionary]:
    """Fully-qualified name -> the field dictionary that holds its /V."""
    form = pdf.Root.get("/AcroForm")
    found: Dict[str, Dictionary] = {}
    if not isinstance(form, Dictionary):
        return found
    seen = set()

    def walk(node, prefix):
        if not isinstance(node, Dictionary) or node.objgen in seen:
            return
        seen.add(node.objgen)
        name = prefix
        if "/T" in node:
            name = f"{prefix}.{node.T}" if prefix else str(node.T)
            found.setdefault(name, node)
        for kid in node.get("/Kids") or []:
            walk(kid, name)

    for top in form.get("/Fields") or []:
        walk(top, "")
    return found


def _widgets_of(field: Dictionary) -> List[Dictionary]:
    if field.get("/Subtype") == Name.Widget:
        return [field]
    out = []
    for kid in field.get("/Kids") or []:
        if isinstance(kid, Dictionary) and "/T" not in kid:
            out.append(kid)  # widget kids have no name of their own
    return out


def on_state(widget: Dictionary) -> str:
    """A check box / radio widget's "on" appearance-state name."""
    normal = widget.get("/AP", {}).get("/N") if "/AP" in widget else None
    if isinstance(normal, Dictionary):
        for key in normal.keys():
            if key != "/Off":
                return key[1:]
    return "Yes"


def apply_field_edits(pdf: pikepdf.Pdf, values: Dict[str, str]) -> None:
    """Set these fields' values in `pdf`, drawing new appearances as needed.
    Names that aren't in the form are ignored (a field deleted elsewhere)."""
    if not values:
        return
    fields = _fields_by_name(pdf)
    for name, value in values.items():
        field = fields.get(name)
        if field is None:
            continue
        ft = _inherited(field, "/FT")
        if ft == Name.Btn:
            # `value` is an on-state name, or -- for fields with /Opt -- the
            # export value at that widget's position in /Opt.
            opt = _inherited(field, "/Opt")
            opts = [str(o) for o in opt] if isinstance(opt, Array) else []
            chosen = None
            for n, widget in enumerate(_widgets_of(field)):
                state = on_state(widget)
                wanted = value != "Off" and (value == state or (n < len(opts) and opts[n] == value))
                widget.AS = Name("/" + state) if wanted else Name.Off
                if wanted and chosen is None:
                    chosen = state
            field.V = Name("/" + chosen) if chosen else Name.Off
        elif ft in (Name.Tx, Name.Ch):
            field.V = String(value)
            for widget in _widgets_of(field):
                widget.AP = Dictionary(N=_text_appearance(pdf, widget, value))


def _rgb_ops(values) -> Tuple[str, str]:
    """(stroke op, fill op) for an /MK colour array: grey, RGB or CMYK."""
    v = [float(x) for x in values]
    if len(v) == 1:
        return f"{v[0]:.3f} G", f"{v[0]:.3f} g"
    if len(v) == 3:
        c = " ".join(f"{x:.3f}" for x in v)
        return f"{c} RG", f"{c} rg"
    if len(v) == 4:
        c = " ".join(f"{x:.3f}" for x in v)
        return f"{c} K", f"{c} k"
    return "", ""


def _literal(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _text_appearance(pdf: pikepdf.Pdf, widget: Dictionary, value: str) -> pikepdf.Stream:
    """A text / combo field's appearance: its own background and border
    (/MK), then the value in Helvetica at the field's size and colour,
    aligned per /Q, clipped to the field."""
    x0, y0, x1, y1 = (float(v) for v in widget.Rect)
    w, h = abs(x1 - x0), abs(y1 - y0)
    rotation = 0
    mk = widget.get("/MK")
    if isinstance(mk, Dictionary) and "/R" in mk:
        rotation = int(mk.R) % 360
    if rotation in (90, 270):
        w, h = h, w
    size, color = parse_da(str(_inherited(widget, "/DA") or ""), default_size=0.0)
    flags = int(_inherited(widget, "/Ff") or 0)
    multiline = bool(flags & FLAG_MULTILINE)
    shown = "*" * len(value) if flags & FLAG_PASSWORD else value
    if size <= 0:
        size = auto_font_size(shown, w, h, multiline)
    align = int(_inherited(widget, "/Q") or 0)

    ops: List[bytes] = [b"/Tx BMC q"]
    if isinstance(mk, Dictionary):
        if isinstance(mk.get("/BG"), Array) and len(mk.BG):
            ops.append(f"{_rgb_ops(mk.BG)[1]} 0 0 {w:.3f} {h:.3f} re f".encode())
        if isinstance(mk.get("/BC"), Array) and len(mk.BC):
            border = 1.0
            bs = widget.get("/BS")
            if isinstance(bs, Dictionary) and "/W" in bs:
                border = float(bs.W)
            if border > 0:
                ops.append(f"{_rgb_ops(mk.BC)[0]} {border:g} w {border / 2:.3f} {border / 2:.3f} "
                           f"{w - border:.3f} {h - border:.3f} re S".encode())
    rgb = " ".join(f"{c:.3f}" for c in color)
    ops.append(f"1 1 {max(w - 2, 0):.3f} {max(h - 2, 0):.3f} re W n BT /Helv {size:g} Tf "
               f"{rgb} rg".encode())

    maxlen = int(_inherited(widget, "/MaxLen") or 0)
    if flags & FLAG_COMB and maxlen > 0 and not multiline:
        cell = w / maxlen
        y = (h - size) / 2 + size * 0.23
        for i, ch in enumerate(shown[:maxlen]):
            x = i * cell + (cell - text_width(ch, size)) / 2
            ops.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm ".encode() + _literal(ch) + b" Tj")
    else:
        if multiline:
            lines = layout(shown, size, w - 2).lines
            y = h - 2 - size * 0.77
        else:
            lines = (shown.replace("\n", " "),)
            y = (h - size) / 2 + size * 0.23  # centre the em box vertically
        for line in lines:
            tw = text_width(line, size)
            x = 2.0 if align == 0 else (w - tw) / 2 if align == 1 else w - 2 - tw
            ops.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm ".encode() + _literal(line) + b" Tj")
            y -= size * 1.2
    ops.append(b"ET Q EMC")

    stream = pdf.make_stream(b" ".join(ops))
    stream.Type = Name.XObject
    stream.Subtype = Name.Form
    stream.BBox = Array([0, 0, w, h])
    # /MK /R turns the field's content anticlockwise; draw upright, rotate.
    stream.Matrix = Array(_COUNTER_ROTATION[rotation])
    stream.Resources = Dictionary(Font=Dictionary(Helv=pdf.make_indirect(Dictionary(
        Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
        Encoding=Name.WinAnsiEncoding))))
    return stream


def need_appearances(pdf: pikepdf.Pdf) -> bool:
    form = pdf.Root.get("/AcroForm")
    return isinstance(form, Dictionary) and bool(form.get("/NeedAppearances", False))


def flatten_widgets(src: pikepdf.Pdf, annots: Iterable) -> Tuple[Array, Array]:
    """Stand-alone copies of a page's widgets for the preview overlay.

    Each copy carries its inherited field attributes and full name and drops
    /Parent and /P, so copying it into another document drags in neither
    the field tree nor other pages. Returns (annots, fields) in `src`,
    non-widget entries of `annots` becoming null (indices kept).
    """
    out, fields = [], []
    for d in annots:
        if not (isinstance(d, Dictionary) and d.get("/Subtype") == Name.Widget):
            out.append(None)
            continue
        copy = Dictionary({k: v for k, v in d.items() if k not in ("/Parent", "/P")})
        for key in _INHERITABLE:
            if key not in copy:
                value = _inherited(d, key)
                if value is not None:
                    copy[key] = value
        copy.T = String(full_name(d))
        indirect = src.make_indirect(copy)
        out.append(indirect)
        fields.append(indirect)
    return Array(out), Array(fields)
