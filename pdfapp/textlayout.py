"""Measuring and laying out text boxes in Helvetica.

Free text annotations are drawn in Helvetica, one of the 14 fonts every PDF
viewer must provide, so nothing is embedded and every viewer shows the same
letters. Wrapping and box sizes need its character widths; they're the
standard Adobe metrics (units per 1000 em). Arial was designed with the
same widths, so the Qt editor -- which types in Arial -- wraps where the
saved annotation will.

Helvetica here is WinAnsi-encoded (cp1252): Western European text only.
encodable() says whether a string fits; callers must refuse, not mangle.

No Qt, no pdfium, no pikepdf.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

PADDING = 2.0      # points between the box edge and the text
LEADING = 1.2      # line height as a multiple of the font size
ASCENT = 0.77      # Helvetica ascender, fraction of the size: first baseline
DESCENT = 0.23

_ASCII = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556,
    "8": 556, "9": 556, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556,
    "@": 1015, "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778,
    "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944,
    "X": 667, "Y": 667, "Z": 611, "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556,
    "`": 333, "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556,
    "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722,
    "x": 500, "y": 500, "z": 500, "{": 334, "|": 260, "}": 334, "~": 584,
}
_EXTRA = {  # the cp1252 punctuation people actually type
    "‘": 222, "’": 222, "“": 333, "”": 333, "–": 556,
    "—": 1000, "…": 1000, "•": 350, "€": 556, " ": 278,
    "©": 737, "®": 737, "°": 400, "£": 556, "§": 556,
}


def _width_of(ch: str) -> int:
    if ch in _ASCII:
        return _ASCII[ch]
    if ch in _EXTRA:
        return _EXTRA[ch]
    # Accented Latin letters are as wide as their base letter.
    base = unicodedata.normalize("NFD", ch)[:1]
    return _ASCII.get(base, 556)


# Helvetica-Bold, for stamp labels -- capitals, digits and the punctuation
# stamps use. Anything else falls back to the regular width, near enough
# for sizing a stamp's frame.
_BOLD = {
    " ": 278, "-": 333, ".": 278, ",": 278, ":": 333, "/": 278, "!": 333, "&": 722,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556,
    "8": 556, "9": 556, "A": 722, "B": 722, "C": 722, "D": 722, "E": 667, "F": 611,
    "G": 778, "H": 722, "I": 278, "J": 556, "K": 722, "L": 611, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667,
    "W": 944, "X": 667, "Y": 667, "Z": 611,
}


def bold_text_width(text: str, size: float) -> float:
    return sum(_BOLD.get(ch, _width_of(ch)) for ch in text) * size / 1000.0


def encodable(text: str) -> bool:
    try:
        text.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def text_width(text: str, size: float) -> float:
    return sum(_width_of(ch) for ch in text) * size / 1000.0


def _wrap(line: str, size: float, max_width: float) -> list:
    """Greedy word wrap; a single word too long for the line is split."""
    if text_width(line, size) <= max_width or not line:
        return [line]
    out: list = []
    current = ""
    for word in re.split(r"(\s+)", line):
        candidate = current + word
        if text_width(candidate.rstrip(), size) <= max_width:
            current = candidate
            continue
        if current.strip():
            out.append(current.rstrip())
        current = word.lstrip()
        while text_width(current, size) > max_width and len(current) > 1:
            cut = len(current) - 1
            while cut > 1 and text_width(current[:cut], size) > max_width:
                cut -= 1
            out.append(current[:cut])
            current = current[cut:]
    out.append(current.rstrip())
    return out


@dataclass(frozen=True)
class TextLayout:
    lines: Tuple[str, ...]
    width: float     # box width, points, padding included
    height: float    # box height, points, padding included


def layout(text: str, size: float, box_width: Optional[float] = None) -> TextLayout:
    """Lay out `text` at `size` points.

    box_width None: typewriter -- break only at newlines, box as wide as the
    longest line. Otherwise wrap to fit, box exactly box_width wide.
    """
    raw = text.split("\n") or [""]
    if box_width is None:
        lines = raw
        width = max((text_width(line, size) for line in lines), default=0.0) + 2 * PADDING
        width = max(width, size)  # an empty line still gets a caret's room
    else:
        inner = max(box_width - 2 * PADDING, size)
        lines = [piece for line in raw for piece in _wrap(line, size, inner)]
        width = box_width
    height = len(lines) * size * LEADING + 2 * PADDING
    return TextLayout(tuple(lines), width, height)


_TF = re.compile(r"(\d+(?:\.\d+)?)\s+Tf")
_RG = re.compile(r"(\d*\.?\d+)\s+(\d*\.?\d+)\s+(\d*\.?\d+)\s+rg")
_G = re.compile(r"(\d*\.?\d+)\s+g(?:\s|$)")


def parse_da(da: str, default_size: float = 12.0):
    """(size, (r, g, b)) from a default-appearance string like
    '/Helv 12 Tf 0 0 1 rg'. Size 0 means auto; treat it as the default."""
    size = default_size
    m = _TF.search(da or "")
    if m and float(m.group(1)) > 0:
        size = float(m.group(1))
    color = (0.0, 0.0, 0.0)
    m = _RG.search(da or "")
    if m:
        color = tuple(float(v) for v in m.groups())
    else:
        m = _G.search(da or "")
        if m:
            grey = float(m.group(1))
            color = (grey, grey, grey)
    return size, color
