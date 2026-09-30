"""Annotations as data.

Like page edits, annotation edits never touch the file until save. For every
page the user has changed, AnnotEdits holds that page's complete list of
editable annotations as it should end up: ones from the file (tagged with
their index in the page's /Annots array, `origin`) possibly moved or edited,
and new ones. An existing annotation missing from the list was deleted.
Pages with no entry keep exactly what the file has.

Pages are identified by (file, source page), not position, so annotation
edits survive reordering, deletion and undo of either.

Some annotations are never edited here and pass through untouched: links and
form widgets (other features own those), popups (they belong to their parent
note), and anything flagged hidden. is_editable() is the one definition both
the reader (pdfium) and the writer (pikepdf) use, so they always agree which
entries of /Annots the list describes.

No Qt, no pdfium, no pikepdf.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, Optional, Tuple

from .fields import FieldEdits, FormEdits
from .ocrlayer import OcrEdits
from .pagelist import PageList
from .textlayout import bold_text_width, text_width
from .transform import Rect, normalize_rect

PageKey = Tuple[int, int]  # (file, source page)

PASSTHROUGH_KINDS = frozenset({"Link", "Widget", "Popup", "XFAWidget"})
FLAG_HIDDEN = 2
FLAG_NOVIEW = 32

NOTE_SIZE = 20.0                      # points; the note icon's square
NOTE_YELLOW = (1.0, 0.85, 0.2)

# Text markup: anchored to text, so selected by click but never dragged --
# a highlight moved off its words means nothing.
MARKUP_KINDS = frozenset({"Highlight", "Underline", "StrikeOut", "Squiggly"})
HIGHLIGHT_COLORS = {
    "Yellow": (1.0, 0.92, 0.23),
    "Green": (0.55, 0.93, 0.45),
    "Blue": (0.45, 0.78, 1.0),
    "Pink": (1.0, 0.6, 0.8),
}
MARKUP_DEFAULT_COLORS = {
    "Highlight": HIGHLIGHT_COLORS["Yellow"],
    "Underline": (0.1, 0.45, 0.95),
    "StrikeOut": (0.9, 0.1, 0.1),
}

KIND_NAMES = {
    "Text": "Note", "FreeText": "Text", "Highlight": "Highlight",
    "Underline": "Underline", "StrikeOut": "Strikeout", "Squiggly": "Squiggle",
    "Square": "Rectangle", "Circle": "Ellipse", "Line": "Line", "Ink": "Drawing",
    "Polygon": "Polygon", "PolyLine": "Polyline", "Stamp": "Stamp", "Caret": "Caret",
    "Arrow": "Arrow",
    "FileAttachment": "Attachment",
}

_uids = itertools.count(1)


def is_editable(kind: str, flags: int) -> bool:
    return kind not in PASSTHROUGH_KINDS and not (flags & (FLAG_HIDDEN | FLAG_NOVIEW))


def describe(kind: str) -> str:
    return KIND_NAMES.get(kind, "Annotation")


def describe_annot(annot: "Annot") -> str:
    return "Arrow" if annot.kind == "Line" and annot.arrow else describe(annot.kind)


@dataclass(frozen=True)
class Annot:
    """One editable annotation, in the PDF user space of its page.

    origin: index in the source page's /Annots, or None if created here.
    offset: total translation applied to an existing annotation; the writer
        moves the original's /Rect and geometry (QuadPoints, InkList...) by
        this much. For a new annotation `rect` alone is authoritative.
    edited: contents changed, so the writer rewrites /Contents and /M.
    uid: identity of a created annotation (existing ones use origin).
    reply_to: origin of the annotation this one replies to (/IRT). Deleting
        a comment deletes its replies -- the writer does, so the model must.
    quads: text markup geometry, flat /QuadPoints order: per rectangle
        upper-left, upper-right, lower-left, lower-right (x, y each).
    reshaped: an existing markup whose quads were cut by a newer mark of
        the same kind (see lay_markup), or an existing text box whose text
        was edited; the writer rewrites its geometry and draws it a new
        appearance, keeping everything else.
    font_size, boxed: text boxes (FreeText). boxed draws a border.
    fixed_width: the text wraps to the box's width. Text boxes start with
        one; typewriter text (width = its longest line) gets one when the
        user resizes it. Text boxes from other apps always have one.
    text_rotation: text boxes and stamps: the page's rotation (/Rotate,
        plus edit rotation) when made. The appearance counter-rotates by it
        so the text reads upright as the page is displayed.
    width: stroke width in points (shapes, lines, ink).
    line: Line annotations: (x1, y1, x2, y2); arrow puts a head at x2, y2.
    ink: Ink annotations: strokes, each a flat (x, y, x, y, ...) tuple.
    label, sublabel, stamp_name: stamps: the big word, the small name/date
        line, and the /Name viewers use to identify the stamp type.
    """

    kind: str
    rect: Rect
    contents: str = ""
    author: str = ""
    color: Tuple[float, float, float] = NOTE_YELLOW
    origin: Optional[int] = None
    offset: Tuple[float, float] = (0.0, 0.0)
    edited: bool = False
    uid: int = 0
    reply_to: Optional[int] = None
    quads: Tuple[float, ...] = ()
    reshaped: bool = False
    font_size: float = 12.0
    boxed: bool = False
    fixed_width: bool = False
    text_rotation: int = 0
    width: float = 1.0
    line: Tuple[float, ...] = ()
    arrow: bool = False
    ink: Tuple[Tuple[float, ...], ...] = ()
    label: str = ""
    sublabel: str = ""
    stamp_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "rect", normalize_rect(tuple(float(v) for v in self.rect)))

    @property
    def ident(self) -> Tuple[str, int]:
        return ("o", self.origin) if self.origin is not None else ("n", self.uid)

    @property
    def is_markup(self) -> bool:
        return self.kind in MARKUP_KINDS

    def moved(self, dx: float, dy: float) -> "Annot":
        x0, y0, x1, y1 = self.rect
        return replace(self, rect=(x0 + dx, y0 + dy, x1 + dx, y1 + dy),
                       quads=_shift(self.quads, dx, dy), line=_shift(self.line, dx, dy),
                       ink=tuple(_shift(stroke, dx, dy) for stroke in self.ink),
                       offset=(self.offset[0] + dx, self.offset[1] + dy))

    def with_contents(self, contents: str) -> "Annot":
        return replace(self, contents=contents, edited=True)

    def contains(self, x: float, y: float, slop: float = 2.0) -> bool:
        # Lines and ink by distance to the stroke: a diagonal line's bounding
        # box is mostly empty page, which shouldn't grab it.
        reach = slop + self.width / 2
        if self.line:
            return _near_polyline(self.line, x, y, reach)
        if self.ink:
            return any(_near_polyline(stroke, x, y, reach) for stroke in self.ink)
        if self.quads:
            # Per line: a two-line highlight's bounding box also covers the
            # unmarked ends of both lines, which shouldn't select it.
            q = self.quads
            for i in range(0, len(q) - 7, 8):
                xs, ys = q[i:i + 8:2], q[i + 1:i + 8:2]
                if min(xs) - slop <= x <= max(xs) + slop and min(ys) - slop <= y <= max(ys) + slop:
                    return True
            return False
        x0, y0, x1, y1 = self.rect
        return x0 - slop <= x <= x1 + slop and y0 - slop <= y <= y1 + slop


def _shift(flat: Tuple[float, ...], dx: float, dy: float) -> Tuple[float, ...]:
    return tuple(v + (dx if i % 2 == 0 else dy) for i, v in enumerate(flat))


def _near_polyline(flat: Tuple[float, ...], x: float, y: float, reach: float) -> bool:
    points = list(zip(flat[0::2], flat[1::2]))
    if len(points) == 1:
        points = points * 2
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        vx, vy = bx - ax, by - ay
        length2 = vx * vx + vy * vy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / length2))
        px, py = ax + t * vx - x, ay + t * vy - y
        if px * px + py * py <= reach * reach:
            return True
    return False


def without(annots: Iterable[Annot], gone: Annot) -> Tuple[Annot, ...]:
    """`annots` minus `gone` and every reply to it, however deep."""
    annots = tuple(annots)
    removed = {gone.ident}
    removed_origins = {gone.origin} if gone.origin is not None else set()
    changed = True
    while changed:
        changed = False
        for a in annots:
            if a.ident not in removed and a.reply_to is not None                     and a.reply_to in removed_origins:
                removed.add(a.ident)
                if a.origin is not None:
                    removed_origins.add(a.origin)
                changed = True
    return tuple(a for a in annots if a.ident not in removed)


def new_note(x: float, y: float, contents: str, author: str,
             color: Tuple[float, float, float] = NOTE_YELLOW) -> Annot:
    """A sticky note whose icon's top-left corner is at (x, y), PDF space."""
    return Annot("Text", (x, y - NOTE_SIZE, x + NOTE_SIZE, y), contents=contents,
                 author=author, color=color, uid=next(_uids))


# ---- one layer of each markup kind ------------------------------------------
#
# Marking text that's already marked with the same kind replaces the old mark
# on that text instead of stacking on it: highlights multiply, so stacked ones
# darken without limit until the text is gone. The older mark is cut back --
# split if the new one lands in its middle, removed if it's covered entirely.

_Box = Tuple[float, float, float, float]  # left, bottom, right, top


def _quad_boxes(quads: Tuple[float, ...]):
    """(box, axis_aligned) per quad. Only axis-aligned quads -- ordinary
    horizontal text -- can be cut; anything else is left alone."""
    for i in range(0, len(quads) - 7, 8):
        xs, ys = sorted(quads[i:i + 8:2]), sorted(quads[i + 1:i + 8:2])
        aligned = xs[1] - xs[0] < 0.5 and xs[3] - xs[2] < 0.5             and ys[1] - ys[0] < 0.5 and ys[3] - ys[2] < 0.5
        yield (xs[0], ys[0], xs[3], ys[3]), aligned


def _boxes_to_quads(boxes) -> Tuple[float, ...]:
    quads: list = []
    for left, bottom, right, top in boxes:
        quads += [left, top, right, top, left, bottom, right, bottom]
    return tuple(quads)


def _cut(box: _Box, cutters) -> list:
    """`box` minus the parts on the same line that any cutter covers."""
    pieces = [box]
    for cl, cb, cr, ct in cutters:
        next_pieces = []
        for left, bottom, right, top in pieces:
            overlap = min(top, ct) - max(bottom, cb)
            same_line = overlap > 0.5 * min(top - bottom, ct - cb)
            if not same_line or cr <= left or cl >= right:
                next_pieces.append((left, bottom, right, top))
                continue
            if cl - left > 0.5:
                next_pieces.append((left, bottom, cl, top))
            if right - cr > 0.5:
                next_pieces.append((cr, bottom, right, top))
        pieces = next_pieces
    return pieces


def lay_markup(annots: Iterable[Annot], new: Annot,
               char_boxes: Optional[Iterable[_Box]] = None) -> Tuple[Annot, ...]:
    """Add markup `new` to a page's annotations, one layer per kind.

    char_boxes: the page's character boxes. A piece left over after cutting
    that has no character under it is dropped -- marks drawn wider than
    their text (common from other apps) would otherwise leave slivers of
    the old colour at both ends of a fully re-marked line.
    """
    cutters = [box for box, _ in _quad_boxes(new.quads)]
    centers = None if char_boxes is None else [
        ((l + r) / 2, (b + t) / 2) for l, b, r, t in char_boxes if r > l and t > b]

    def has_text(piece: _Box) -> bool:
        left, bottom, right, top = piece
        return any(left <= x <= right and bottom <= y <= top for x, y in centers)

    kept: list = []
    covered: list = []
    for a in annots:
        if a.kind != new.kind or not a.quads:
            kept.append(a)
            continue
        pieces: list = []
        for box, aligned in _quad_boxes(a.quads):
            if not aligned:
                pieces.append(box)
                continue
            cut = _cut(box, cutters)
            if cut != [box] and centers is not None:
                cut = [piece for piece in cut if has_text(piece)]
            pieces += cut
        if pieces == [box for box, _ in _quad_boxes(a.quads)]:
            kept.append(a)
        elif not pieces:
            covered.append(a)
        else:
            kept.append(replace(
                a, quads=_boxes_to_quads(pieces), reshaped=True,
                rect=(min(p[0] for p in pieces), min(p[1] for p in pieces),
                      max(p[2] for p in pieces), max(p[3] for p in pieces))))

    # A mark covered completely goes -- but its comment moves to the new
    # mark rather than vanishing with it.
    comments = [a.contents for a in covered if a.contents]
    if comments and not new.contents:
        new = replace(new, contents="\n\n".join(comments))
    result = tuple(kept)
    for gone in covered:
        result = without(result + (gone,), gone)  # takes its replies too
    return result + (new,)


TEXT_COLORS = {"Black": (0.0, 0.0, 0.0), "Blue": (0.1, 0.25, 0.8), "Red": (0.8, 0.1, 0.1)}
TEXT_SIZES = (8, 10, 12, 14, 18, 24)


def text_box_size(annot: Annot) -> Tuple[float, float]:
    """(width, height) of a text box as read on screen: its Rect, turned
    back through the page rotation it was typed at."""
    x0, y0, x1, y1 = annot.rect
    w, h = x1 - x0, y1 - y0
    return (w, h) if annot.text_rotation % 180 == 0 else (h, w)


def new_text(rect: Rect, contents: str, author: str, size: float,
             color: Tuple[float, float, float], boxed: bool, rotation: int) -> Annot:
    return Annot("FreeText", rect, contents=contents, author=author, color=color,
                 font_size=size, boxed=boxed, fixed_width=boxed, text_rotation=rotation,
                 uid=next(_uids))


# ---- shapes, ink, stamps ------------------------------------------------------

SHAPE_COLORS = {"Red": (0.85, 0.1, 0.1), "Blue": (0.1, 0.3, 0.85),
                "Green": (0.1, 0.6, 0.2), "Black": (0.0, 0.0, 0.0)}
SHAPE_WIDTHS = (1, 2, 4, 8)
ARROW_LENGTH = 3.5   # arrowhead length, in stroke widths (at least 6pt)


def _bbox(points, pad: float) -> Rect:
    xs, ys = points[0::2], points[1::2]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def new_shape(kind: str, rect: Rect, author: str, color, width: float) -> Annot:
    """A Square or Circle drawn inside `rect` (the stroke stays inside)."""
    if kind not in ("Square", "Circle"):
        raise ValueError(kind)
    return Annot(kind, rect, author=author, color=color, width=width, uid=next(_uids))


def new_line(x1: float, y1: float, x2: float, y2: float, author: str, color, width: float,
             arrow: bool) -> Annot:
    # Rect must hold the stroke's thickness and the arrowhead too.
    pad = width / 2 + (max(6.0, width * ARROW_LENGTH) if arrow else 0.0) + 1
    return Annot("Line", _bbox((x1, y1, x2, y2), pad), author=author, color=color, width=width,
                 line=(x1, y1, x2, y2), arrow=arrow, uid=next(_uids))


def new_ink(stroke: Tuple[float, ...], author: str, color, width: float) -> Annot:
    return Annot("Ink", _bbox(stroke, width / 2 + 1), author=author, color=color, width=width,
                 ink=(tuple(stroke),), uid=next(_uids))


# (label, colour, carries name and date, /Name)
STAMPS = (
    ("APPROVED", (0.1, 0.55, 0.2), True, "Approved"),
    ("REVIEWED", (0.1, 0.3, 0.8), True, "Reviewed"),
    ("RECEIVED", (0.1, 0.3, 0.8), True, "Received"),
    ("DRAFT", (0.35, 0.35, 0.45), False, "Draft"),
    ("CONFIDENTIAL", (0.8, 0.1, 0.1), False, "Confidential"),
    ("REJECTED", (0.8, 0.1, 0.1), False, "NotApproved"),
)
STAMP_SIZE = 16.0     # label font size, points
STAMP_SUBSIZE = 7.0
STAMP_PADDING = 6.0


def stamp_size(label: str, sublabel: str) -> Tuple[float, float]:
    """(width, height) of a stamp as read on screen."""
    inner = max(bold_text_width(label, STAMP_SIZE), text_width(sublabel, STAMP_SUBSIZE))
    height = STAMP_SIZE + (STAMP_SUBSIZE * 1.4 if sublabel else 0.0)
    return inner + 2 * STAMP_PADDING, height + 2 * STAMP_PADDING


def new_stamp(label: str, sublabel: str, x: float, y: float, author: str, color,
              rotation: int, name: str) -> Annot:
    """A stamp centred on (x, y), reading upright on a page shown at `rotation`."""
    w, h = stamp_size(label, sublabel)
    if rotation % 180:
        w, h = h, w
    rect = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
    return Annot("Stamp", rect, contents=label, author=author, color=color,
                 text_rotation=rotation, label=label, sublabel=sublabel, stamp_name=name,
                 uid=next(_uids))


def new_markup(kind: str, rects: Iterable[Rect], author: str,
               color: Optional[Tuple[float, float, float]] = None) -> Annot:
    """A Highlight / Underline / StrikeOut over text rectangles (PDF space)."""
    if kind not in MARKUP_DEFAULT_COLORS:
        raise ValueError(f"not a text markup kind: {kind}")
    quads: list = []
    xs: list = []
    ys: list = []
    for rect in rects:
        left, bottom, right, top = normalize_rect(rect)
        quads += [left, top, right, top, left, bottom, right, bottom]
        xs += [left, right]
        ys += [bottom, top]
    if not quads:
        raise ValueError("no text to mark")
    return Annot(kind, (min(xs), min(ys), max(xs), max(ys)), author=author,
                 color=color or MARKUP_DEFAULT_COLORS[kind], quads=tuple(quads),
                 uid=next(_uids))


class AnnotEdits:
    """Immutable PageKey -> tuple of Annot, for pages whose annotations changed."""

    __slots__ = ("_pages",)

    def __init__(self, pages: Optional[Dict[PageKey, Tuple[Annot, ...]]] = None):
        self._pages: Dict[PageKey, Tuple[Annot, ...]] = dict(pages or {})

    def get(self, key: PageKey) -> Optional[Tuple[Annot, ...]]:
        return self._pages.get(key)

    def with_page(self, key: PageKey, annots: Iterable[Annot]) -> "AnnotEdits":
        pages = dict(self._pages)
        pages[key] = tuple(annots)
        return AnnotEdits(pages)

    def items(self):
        return self._pages.items()

    def __bool__(self) -> bool:
        return bool(self._pages)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AnnotEdits) and self._pages == other._pages

    def __hash__(self) -> int:
        return hash(frozenset(self._pages.items()))

    def __repr__(self) -> str:
        return f"AnnotEdits({self._pages!r})"


@dataclass(frozen=True)
class DocState:
    """Everything the undo history snapshots: pages, annotation edits, form
    field values, form structure, recognised (OCR) text layers, and the
    password protection to save with (security.py: None keeps the file's)."""

    pages: PageList
    annots: AnnotEdits = field(default_factory=AnnotEdits)
    fields: FieldEdits = field(default_factory=FieldEdits)
    forms: FormEdits = field(default_factory=FormEdits)
    ocr: OcrEdits = field(default_factory=OcrEdits)
    protection: Optional[str] = None
