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

from .pagelist import PageList
from .transform import Rect, normalize_rect

PageKey = Tuple[int, int]  # (file, source page)

PASSTHROUGH_KINDS = frozenset({"Link", "Widget", "Popup", "XFAWidget"})
FLAG_HIDDEN = 2
FLAG_NOVIEW = 32

NOTE_SIZE = 20.0                      # points; the note icon's square
NOTE_YELLOW = (1.0, 0.85, 0.2)

KIND_NAMES = {
    "Text": "Note", "FreeText": "Text Box", "Highlight": "Highlight",
    "Underline": "Underline", "StrikeOut": "Strikeout", "Squiggly": "Squiggle",
    "Square": "Rectangle", "Circle": "Ellipse", "Line": "Line", "Ink": "Drawing",
    "Polygon": "Polygon", "PolyLine": "Polyline", "Stamp": "Stamp", "Caret": "Caret",
    "FileAttachment": "Attachment",
}

_uids = itertools.count(1)


def is_editable(kind: str, flags: int) -> bool:
    return kind not in PASSTHROUGH_KINDS and not (flags & (FLAG_HIDDEN | FLAG_NOVIEW))


def describe(kind: str) -> str:
    return KIND_NAMES.get(kind, "Annotation")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "rect", normalize_rect(tuple(float(v) for v in self.rect)))

    @property
    def ident(self) -> Tuple[str, int]:
        return ("o", self.origin) if self.origin is not None else ("n", self.uid)

    def moved(self, dx: float, dy: float) -> "Annot":
        x0, y0, x1, y1 = self.rect
        return replace(self, rect=(x0 + dx, y0 + dy, x1 + dx, y1 + dy),
                       offset=(self.offset[0] + dx, self.offset[1] + dy))

    def with_contents(self, contents: str) -> "Annot":
        return replace(self, contents=contents, edited=True)

    def contains(self, x: float, y: float, slop: float = 2.0) -> bool:
        x0, y0, x1, y1 = self.rect
        return x0 - slop <= x <= x1 + slop and y0 - slop <= y <= y1 + slop


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
    """Everything the undo history snapshots: pages and annotation edits."""

    pages: PageList
    annots: AnnotEdits = field(default_factory=AnnotEdits)
