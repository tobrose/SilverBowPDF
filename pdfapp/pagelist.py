"""Page edits as data: which source pages, in what order, rotated how much.

Page operations never touch the file until save. The edited document is just
a tuple of PageRefs pointing back into the original, which pdfium keeps
rendering read-only and pikepdf rebuilds on save. That makes every operation
O(pages) on a tuple of small structs, and undo a matter of keeping the old
tuple -- no re-serialising, no reloading pdfium, no way for the preview and
the file to drift apart mid-session.

No Qt, no pdfium, no pikepdf. Pure data, tested headless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Iterable, List, Optional, Sequence, Tuple, TypeVar

from .transform import normalize_rotation


@dataclass(frozen=True)
class PageRef:
    """One page of the edited document.

    source: 0-based index of the page in the file as it was opened.
    rotation: clockwise degrees added on top of the page's own /Rotate.
    """

    source: int
    rotation: int = 0


class PageList:
    """Immutable ordered list of PageRefs. Every edit returns a new PageList."""

    __slots__ = ("_refs",)

    def __init__(self, refs: Iterable[PageRef]):
        self._refs: Tuple[PageRef, ...] = tuple(refs)

    @classmethod
    def identity(cls, page_count: int) -> "PageList":
        return cls(PageRef(i) for i in range(page_count))

    # ---- access --------------------------------------------------------

    def __len__(self) -> int:
        return len(self._refs)

    def __getitem__(self, index: int) -> PageRef:
        return self._refs[index]

    def __iter__(self):
        return iter(self._refs)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PageList) and self._refs == other._refs

    def __hash__(self) -> int:
        return hash(self._refs)

    def __repr__(self) -> str:
        return f"PageList({list(self._refs)!r})"

    def is_identity(self, page_count: int) -> bool:
        return self == PageList.identity(page_count)

    def _check(self, indices: Sequence[int]) -> List[int]:
        if not indices:
            raise ValueError("no pages selected")
        unique = sorted(set(indices))
        for i in unique:
            if not 0 <= i < len(self._refs):
                raise IndexError(f"page {i} out of range (0..{len(self._refs) - 1})")
        return unique

    # ---- edits ---------------------------------------------------------

    def rotated(self, indices: Sequence[int], delta: int) -> "PageList":
        chosen = set(self._check(indices))
        return PageList(
            PageRef(r.source, normalize_rotation(r.rotation + delta)) if i in chosen else r
            for i, r in enumerate(self._refs)
        )

    def deleted(self, indices: Sequence[int]) -> "PageList":
        chosen = set(self._check(indices))
        # A PDF with zero pages is invalid, and "delete everything" is far more
        # likely a mis-click than an intent.
        if len(chosen) >= len(self._refs):
            raise ValueError("cannot delete every page in the document")
        return PageList(r for i, r in enumerate(self._refs) if i not in chosen)

    def moved(self, indices: Sequence[int], to: int) -> "PageList":
        """Move the selected pages, keeping their relative order, so the first
        of them lands at position `to` in the result."""
        chosen = self._check(indices)
        moving = [self._refs[i] for i in chosen]
        rest = [r for i, r in enumerate(self._refs) if i not in set(chosen)]
        to = max(0, min(len(rest), to))
        return PageList(rest[:to] + moving + rest[to:])


T = TypeVar("T")


class History(Generic[T]):
    """Snapshot undo stack.

    Holds whole states rather than commands. For a PageList that's a tuple of
    a few hundred small structs at worst, and a snapshot can't be inverted
    wrongly -- which a hand-written command's undo() can.
    """

    def __init__(self, initial: T):
        self._states: List[T] = [initial]
        self._labels: List[str] = [""]
        self._pos = 0
        self._saved_pos: Optional[int] = 0

    @property
    def current(self) -> T:
        return self._states[self._pos]

    def push(self, state: T, label: str) -> None:
        if state == self.current:
            return
        del self._states[self._pos + 1:]
        del self._labels[self._pos + 1:]
        # Truncating the redo branch can orphan the saved state for good.
        if self._saved_pos is not None and self._saved_pos > self._pos:
            self._saved_pos = None
        self._states.append(state)
        self._labels.append(label)
        self._pos += 1

    def can_undo(self) -> bool:
        return self._pos > 0

    def can_redo(self) -> bool:
        return self._pos < len(self._states) - 1

    def undo_label(self) -> str:
        return self._labels[self._pos] if self.can_undo() else ""

    def redo_label(self) -> str:
        return self._labels[self._pos + 1] if self.can_redo() else ""

    def undo(self) -> T:
        if not self.can_undo():
            raise IndexError("nothing to undo")
        self._pos -= 1
        return self.current

    def redo(self) -> T:
        if not self.can_redo():
            raise IndexError("nothing to redo")
        self._pos += 1
        return self.current

    @property
    def dirty(self) -> bool:
        return self._pos != self._saved_pos

    def mark_saved(self) -> None:
        self._saved_pos = self._pos
