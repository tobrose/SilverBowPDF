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

No Qt, no pdfium, no pikepdf.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .textlayout import text_width
from .transform import Rect, normalize_rect

FieldKey = Tuple[int, str]  # (file, fully-qualified name)

TEXT, CHECKBOX, RADIO, COMBO = "text", "checkbox", "radio", "combo"
FILLABLE = (TEXT, CHECKBOX, RADIO, COMBO)

FLAG_READONLY = 1
FLAG_MULTILINE = 1 << 12
FLAG_PASSWORD = 1 << 13
FLAG_COMB = 1 << 24


@dataclass(frozen=True)
class Widget:
    """One visible piece of a field on a page, as the file has it.

    origin: index in the page's /Annots. value/checked are the FILE's; the
    current state comes from FieldEdits (see current_value / is_on).
    on_state: for buttons, this widget's "on" appearance-state name.
    """

    origin: int
    rect: Rect
    name: str
    kind: str              # one of FILLABLE, or "other" (push button, signature, list)
    flags: int
    value: str
    on_state: str = ""
    options: Tuple[str, ...] = ()
    font_size: float = 0.0  # 0: auto

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


def auto_font_size(value: str, width: float, height: float, multiline: bool) -> float:
    """What a field with font size 0 ("auto") uses: fit the height, then
    shrink until the text fits the width. Multi-line fields just use 10."""
    if multiline:
        return 10.0
    size = max(4.0, min(12.0, (height - 4) / 1.2))
    while size > 4.0 and text_width(value, size) > width - 4:
        size -= 0.5
    return size


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
