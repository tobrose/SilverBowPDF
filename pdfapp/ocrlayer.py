"""The invisible text layer OCR adds to a scanned page. No libs.

Recognised words are drawn as invisible text (render mode 3) over the page
image, each word stretched to exactly the box it covers there, so text
selection, search and highlights line up with the ink. This is what
scanners and Acrobat's "Recognize Text" write too.

The font is a Type0 font with Identity-H encoding and a ToUnicode map that
maps every code to itself: each character is written as its own Unicode
value (BMP), so any language's text round-trips. No glyphs are embedded --
nothing is ever drawn with it. Every glyph is 0.5 em wide and 1 em tall,
sitting on the baseline; a word's text matrix scales that to its box.

OcrWord.matrix is that text matrix (a b c d e f), in PDF user space: text
space (x along the line, in half-em steps per character; y up, 1 = the
line's height) to page coordinates. The same words build the preview's
text layer (text_layer_pdf, for pdfium) and the saved one (writer.py).
"""

from __future__ import annotations

from typing import Dict, Iterable, NamedTuple, Optional, Tuple

PageKey = Tuple[int, int]  # (file, source page), as annots.PageKey

GLYPH_WIDTH = 500  # thousandths of an em, every glyph (font /DW)
FONT_NAME = "GlyphLessFont"


class OcrWord(NamedTuple):
    text: str        # the word, plus the space after it when another word follows on its line
    matrix: Tuple[float, float, float, float, float, float]


class OcrEdits:
    """Immutable PageKey -> tuple of OcrWord, for pages given a text layer."""

    __slots__ = ("_pages",)

    def __init__(self, pages: Optional[Dict[PageKey, Tuple[OcrWord, ...]]] = None):
        self._pages: Dict[PageKey, Tuple[OcrWord, ...]] = dict(pages or {})

    def get(self, key: PageKey) -> Optional[Tuple[OcrWord, ...]]:
        return self._pages.get(key)

    def with_pages(self, pages: Dict[PageKey, Iterable[OcrWord]]) -> "OcrEdits":
        merged = dict(self._pages)
        merged.update({k: tuple(v) for k, v in pages.items()})
        return OcrEdits(merged)

    def items(self):
        return self._pages.items()

    def __bool__(self) -> bool:
        return bool(self._pages)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OcrEdits) and self._pages == other._pages

    def __hash__(self) -> int:
        return hash(frozenset(self._pages.items()))

    def __repr__(self) -> str:
        return f"OcrEdits({len(self._pages)} pages)"


def _hex(text: str) -> str:
    # BMP only: a character outside it can't be one 2-byte code. OCR engines
    # don't produce those for text scripts in practice; drop rather than garble.
    return "".join(f"{ord(ch):04X}" for ch in text if ord(ch) <= 0xFFFF)


def _num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def content(words: Iterable[OcrWord], font: str) -> bytes:
    """The content stream drawing `words` invisibly with font resource `font`."""
    out = [f"BT 3 Tr /{font} 1 Tf"]
    for word in words:
        code = _hex(word.text)
        if code:
            out.append(" ".join(_num(v) for v in word.matrix) + f" Tm <{code}> Tj")
    out.append("ET")
    return "\n".join(out).encode("ascii")


TO_UNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""

# The font's dictionaries, as (key, value) source text, shared by the preview
# PDF below and writer.py (which builds the same objects with pikepdf).
FONT_DESCRIPTOR = {
    "/Type": "/FontDescriptor", "/FontName": f"/{FONT_NAME}", "/Flags": 5,
    "/FontBBox": [0, 0, GLYPH_WIDTH, 1000], "/ItalicAngle": 0, "/Ascent": 1000,
    "/Descent": 0, "/CapHeight": 1000, "/StemV": 80,
}


def text_layer_pdf(words: Iterable[OcrWord], media, crop, rotate: int) -> bytes:
    """A one-page PDF holding only the text layer, with the page's own
    geometry -- what pdfium reads the preview's text from, so selection and
    search work before the file is saved. Written by hand: no libs here."""
    def box(b) -> str:
        return "[" + " ".join(_num(v) for v in b) + "]"

    stream = content(words, "F0")
    fd = " ".join(f"{k} {box(v) if isinstance(v, list) else v}"
                  for k, v in FONT_DESCRIPTOR.items())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox {box(media)} /CropBox {box(crop)} "
         f"/Rotate {int(rotate) % 360} /Resources << /Font << /F0 5 0 R >> >> "
         f"/Contents 4 0 R >>").encode(),
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        (f"<< /Type /Font /Subtype /Type0 /BaseFont /{FONT_NAME} /Encoding /Identity-H "
         f"/DescendantFonts [6 0 R] /ToUnicode 8 0 R >>").encode(),
        (f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{FONT_NAME} "
         f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
         f"/FontDescriptor 7 0 R /DW {GLYPH_WIDTH} /CIDToGIDMap /Identity >>").encode(),
        f"<< {fd} >>".encode(),
        f"<< /Length {len(TO_UNICODE)} >>\nstream\n".encode() + TO_UNICODE + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def word_matrix(start, along, up, chars: int) -> Tuple[float, float, float, float, float, float]:
    """Text matrix for a word of `chars` characters whose baseline starts at
    `start` and runs to `along`, and whose top-left is `up` (PDF points)."""
    (x0, y0), (x1, y1), (x2, y2) = start, along, up
    per_char = GLYPH_WIDTH / 1000 * max(chars, 1)
    return ((x1 - x0) / per_char, (y1 - y0) / per_char, x2 - x0, y2 - y0, x0, y0)
