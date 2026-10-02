"""Form fields as data: what a page's fields are, and what the user filled in.

Field values follow the same rule as every other edit: nothing touches the
file until save. FieldEdits maps (file, fully-qualified field name) to the
new value; fields without an entry keep what the file has.

Values are strings for every kind:
  text, combo box   the text
  check box, radio  the "on" appearance-state name of the widget that is
                    on (/AS, e.g. "Yes"), or "Off"
Using the appearance-state name for buttons -- not the export value -- is
what lets the writer set /AS on each widget without guessing.

Form STRUCTURE edits (fields added, moved, resized, deleted, their
properties changed) are FormEdits, shaped like annotation edits: for each
changed page, the complete list of its widgets as they should end up.
Existing widgets carry `origin` (their /Annots index); new ones have none;
an existing widget missing from the list was deleted.

No Qt, no pdfium, no pikepdf.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from .textlayout import text_width
from .transform import Rect, normalize_rect

FieldKey = Tuple[int, str]  # (file, fully-qualified name)

TEXT, CHECKBOX, RADIO, COMBO = "text", "checkbox", "radio", "combo"
FILLABLE = (TEXT, CHECKBOX, RADIO, COMBO)
SIGNATURE = "signature"

FLAG_READONLY = 1
FLAG_REQUIRED = 2
FLAG_COMBO = 1 << 17
FLAG_MULTILINE = 1 << 12
FLAG_PASSWORD = 1 << 13
FLAG_COMB = 1 << 24


@dataclass(frozen=True)
class Widget:
    """One visible piece of a field on a page, as the file has it.

    origin: index in the page's /Annots, None for a field created here.
    value/checked are the FILE's; the current state comes from FieldEdits
    (see current_value / is_on).
    on_state: for buttons, this widget's "on" appearance-state name.
    changed: an existing widget whose rect or properties were edited; the
        writer rewrites them and redraws its appearance.
    uid: identity of a field created here.
    """

    origin: Optional[int]
    rect: Rect
    name: str
    kind: str              # one of FILLABLE, SIGNATURE, or "other" (push button, list)
    flags: int
    value: str
    on_state: str = ""
    options: Tuple[str, ...] = ()
    font_size: float = 0.0  # 0: auto
    changed: bool = False
    uid: int = 0
    # A signature field with a signature in it (/V on the widget itself; a
    # kid widget's /V is on its parent, which pdfium can't reach -- the view
    # also has the signed field names from checking the signatures).
    signed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "rect", normalize_rect(tuple(float(v) for v in self.rect)))

    @property
    def fillable(self) -> bool:
        return self.kind in FILLABLE and not self.flags & FLAG_READONLY

    @property
    def multiline(self) -> bool:
        return self.kind == TEXT and bool(self.flags & FLAG_MULTILINE)

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.rect
        return x0 <= x <= x1 and y0 <= y <= y1

    def short_name(self) -> str:
        return self.name.rsplit(".", 1)[-1] or "field"

    @property
    def ident(self):
        return ("o", self.origin) if self.origin is not None else ("n", self.uid)

    def moved_to(self, rect: Rect) -> "Widget":
        return replace(self, rect=rect, changed=True)

    def renamed(self, last: str) -> "Widget":
        prefix = self.name.rsplit(".", 1)[0] + "." if "." in self.name else ""
        return replace(self, name=prefix + last, changed=True)


def auto_font_size(value: str, width: float, height: float, multiline: bool) -> float:
    """What a field with font size 0 ("auto") uses: fit the height, then
    shrink until the text fits the width. Multi-line fields just use 10."""
    if multiline:
        return 10.0
    size = max(4.0, min(12.0, (height - 4) / 1.2))
    while size > 4.0 and text_width(value, size) > width - 4:
        size -= 0.5
    return size


_uids = itertools.count(1)
NEW_FIELD_SIZES = {TEXT: (150.0, 20.0), CHECKBOX: (12.0, 12.0), COMBO: (120.0, 20.0)}
NEW_FIELD_PREFIX = {TEXT: "Text", CHECKBOX: "Check", COMBO: "Dropdown"}


def new_field(kind: str, name: str, rect: Rect, multiline: bool = False,
              options: Tuple[str, ...] = ()) -> Widget:
    flags = FLAG_MULTILINE if multiline and kind == TEXT else 0
    if kind == COMBO:
        flags |= FLAG_COMBO
    value = "Off" if kind == CHECKBOX else ""
    return Widget(None, rect, name, kind, flags, value,
                  on_state="Yes" if kind == CHECKBOX else "", options=tuple(options),
                  font_size=0.0, uid=next(_uids))


def unique_name(kind: str, taken) -> str:
    """"Text 1", "Text 2"... -- the first not already a field name."""
    n = 1
    while f"{NEW_FIELD_PREFIX[kind]} {n}" in taken:
        n += 1
    return f"{NEW_FIELD_PREFIX[kind]} {n}"


class FormEdits:
    """Immutable PageKey -> tuple of Widget, for pages whose fields changed."""

    __slots__ = ("_pages",)

    def __init__(self, pages=None):
        self._pages = dict(pages or {})

    def get(self, key):
        return self._pages.get(key)

    def with_page(self, key, widgets) -> "FormEdits":
        pages = dict(self._pages)
        pages[key] = tuple(widgets)
        return FormEdits(pages)

    def items(self):
        return self._pages.items()

    def __bool__(self) -> bool:
        return bool(self._pages)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FormEdits) and self._pages == other._pages

    def __hash__(self) -> int:
        return hash(frozenset(self._pages.items()))

    def __repr__(self) -> str:
        return f"FormEdits({self._pages!r})"


class FieldEdits:
    """Immutable (file, field name) -> value."""

    __slots__ = ("_values",)

    def __init__(self, values: Optional[Dict[FieldKey, str]] = None):
        self._values: Dict[FieldKey, str] = dict(values or {})

    def get(self, key: FieldKey) -> Optional[str]:
        return self._values.get(key)

    def with_value(self, key: FieldKey, value: str) -> "FieldEdits":
        values = dict(self._values)
        values[key] = value
        return FieldEdits(values)

    def for_file(self, file: int) -> Dict[str, str]:
        return {name: v for (f, name), v in self._values.items() if f == file}

    def items(self):
        return self._values.items()

    def renamed(self, file: int, old: str, new: str) -> "FieldEdits":
        """Carry a filled-in value over when its field is renamed."""
        if (file, old) not in self._values:
            return self
        values = dict(self._values)
        values[(file, new)] = values.pop((file, old))
        return FieldEdits(values)

    def __bool__(self) -> bool:
        return bool(self._values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FieldEdits) and self._values == other._values

    def __hash__(self) -> int:
        return hash(frozenset(self._values.items()))

    def __repr__(self) -> str:
        return f"FieldEdits({self._values!r})"


def current_value(widget: Widget, file: int, edits: FieldEdits) -> str:
    edited = edits.get((file, widget.name))
    return widget.value if edited is None else edited


def is_on(widget: Widget, file: int, edits: FieldEdits) -> bool:
    """A check box or radio widget is on when its field's value names its
    own on-state -- which also turns on same-named widgets in unison."""
    return bool(widget.on_state) and current_value(widget, file, edits) == widget.on_state
