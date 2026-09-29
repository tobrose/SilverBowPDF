"""Generate test PDFs into samples/ (gitignored).

    python scripts/make_test_pdfs.py

Every page says what it is and has an arrow pointing to its own top, so a
wrong rotation, a lost page or a shuffled order is visible at a glance.

  test-pages.pdf      12 pages: mixed sizes, pre-rotated pages, an offset
                      CropBox, form fields, a scan-sized image, bookmarks.
  test-protected.pdf  Same file, password "test".
  test-long.pdf       300 plain pages, for scrolling and many-page edits.
  test-annotations.pdf  3 pages of existing annotations -- a note with a
                      popup and a reply, a rectangle, a highlight, a hidden
                      note, a form field, a rotated page. The same file the
                      annotation tests use.
"""

from __future__ import annotations

import math
import sys
import zlib
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

OUT = Path(__file__).resolve().parent.parent / "samples"

LETTER, LEGAL, A4 = (612, 792), (612, 1008), (595, 842)

# (label, (w, h), /Rotate, special)
PAGES = [
    ("Letter portrait", LETTER, 0, None),
    ("Letter landscape", (792, 612), 0, None),
    ("A4 portrait", A4, 0, None),
    ("Scanned sideways (/Rotate 90)", LETTER, 90, None),
    ("Legal portrait", LEGAL, 0, None),
    ("A4 landscape", (842, 595), 0, None),
    ("Offset CropBox", LETTER, 0, "crop"),
    ("Upside down (/Rotate 180)", LETTER, 180, None),
    ("Index card 5x3 in", (360, 216), 0, None),
    ("Form fields", LETTER, 0, "form"),
    ("Scan-sized image, 300 DPI", LETTER, 0, "image"),
    ("Rotated /Rotate 270", LETTER, 270, None),
]

# One pale tint per page so a reordered page is recognisable at thumbnail size.
TINTS = [
    (1.0, .90, .90), (.90, 1.0, .90), (.90, .90, 1.0), (1.0, 1.0, .85),
    (1.0, .90, 1.0), (.85, 1.0, 1.0), (1.0, .95, .85), (.92, .92, .92),
    (.95, .85, 1.0), (.85, .95, .85), (1.0, 1.0, 1.0), (.85, .90, 1.0),
]


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text(x, y, size, text, font="F1"):
    return f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({_esc(text)}) Tj ET\n"


def _page_content(num: int, total: int, label: str, w: float, h: float,
                  x0: float = 0, y0: float = 0) -> str:
    """Draw inside the box (x0, y0, x0+w, y0+h)."""
    r, g, b = TINTS[(num - 1) % len(TINTS)]
    cx, cy = x0 + w / 2, y0 + h / 2
    big = min(w, h) * 0.45
    s = f"{r} {g} {b} rg {x0} {y0} {w} {h} re f\n"
    s += f"0 0 0 RG 2 w {x0 + 10} {y0 + 10} {w - 20} {h - 20} re S\n"
    # "This way up" arrow at the top centre.
    a = min(w, h) * 0.06
    top = y0 + h - 20
    s += (f"0.8 0.1 0.1 rg {cx} {top} m {cx - a} {top - a * 1.4} l "
          f"{cx + a} {top - a * 1.4} l h f\n")
    s += f"{cx - a * 0.35} {top - a * 3} {a * 0.7} {a * 1.7} re f\n"
    s += "0 0 0 rg\n"
    num_text = str(num)
    s += _text(cx - big * 0.28 * len(num_text), cy - big * 0.35, big, num_text, "F2")
    small = max(8, min(w, h) * 0.035)
    s += _text(x0 + 24, y0 + 28 + small * 1.5, small, f"Page {num} of {total}")
    s += _text(x0 + 24, y0 + 28, small, label)
    corner = max(6, small * 0.7)
    s += _text(x0 + 16, y0 + h - 16 - corner, corner, "TOP LEFT")
    s += _text(x0 + w - 16 - corner * 8, y0 + 18, corner, "BOTTOM RIGHT")
    return s


def _fonts(pdf):
    return Dictionary(
        F1=pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica,
                                        Encoding=Name.WinAnsiEncoding)),
        F2=pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name("/Helvetica-Bold"),
                                        Encoding=Name.WinAnsiEncoding)),
    )


def _scan_image(pdf, px_w: int, px_h: int):
    """A greyscale 'scan': paper texture plus ruled lines, flate-compressed."""
    rows = bytearray()
    for y in range(px_h):
        line = y % 90 < 3
        for x in range(px_w):
            if line and 150 < x < px_w - 150:
                v = 60
            else:
                v = 235 + int(12 * math.sin(x * 0.05) * math.cos(y * 0.031))
            rows.append(max(0, min(255, v)))
    stream = pdf.make_stream(zlib.compress(bytes(rows), 6))
    stream.Type, stream.Subtype = Name.XObject, Name.Image
    stream.Width, stream.Height = px_w, px_h
    stream.ColorSpace, stream.BitsPerComponent = Name.DeviceGray, 8
    stream.Filter = Name.FlateDecode
    return stream


def _add_form_fields(pdf, page, fonts):
    """A text field and a checkbox, with NeedAppearances so pdfium draws them."""
    helv = fonts.F1
    text_field = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx,
        T=String("name"), V=String("Type here once form filling lands"),
        Rect=Array([100, 190, 500, 220]), DA=String("/Helv 12 Tf 0 g"),
        F=4, P=page.obj,
        MK=Dictionary(BC=Array([0, 0, 0]), BG=Array([1, 1, 1])),
    ))
    check = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Widget, FT=Name.Btn,
        T=String("agree"), V=Name.Yes, AS=Name.Yes,
        Rect=Array([100, 140, 118, 158]), DA=String("/ZaDb 12 Tf 0 g"),
        F=4, P=page.obj,
        MK=Dictionary(BC=Array([0, 0, 0]), BG=Array([1, 1, 1]), CA=String("4")),
    ))
    page.obj.Annots = Array([text_field, check])
    pdf.Root.AcroForm = Dictionary(
        Fields=Array([text_field, check]), NeedAppearances=True,
        DA=String("/Helv 0 Tf 0 g"),
        DR=Dictionary(Font=Dictionary(
            Helv=helv,
            ZaDb=pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                              BaseFont=Name.ZapfDingbats)),
        )),
    )


def build_main(path: Path, encryption=None) -> None:
    pdf = pikepdf.Pdf.new()
    fonts = _fonts(pdf)
    total = len(PAGES)
    for num, (label, (w, h), rotate, special) in enumerate(PAGES, start=1):
        pdf.add_blank_page(page_size=(w, h))
        page = pdf.pages[-1]
        resources = Dictionary(Font=fonts)
        content = _page_content(num, total, label, w, h)

        if special == "crop":
            # Visible area starts at (72, 72). Everything outside is a red
            # band that should never be on screen.
            page.CropBox = Array([72, 72, w - 72, h - 72])
            content = f"1 0 0 rg 0 0 {w} {h} re f\n" + _page_content(
                num, total, label + " -- red means crop is broken", w - 144, h - 144, 72, 72)
        elif special == "image":
            resources.XObject = Dictionary(Im1=_scan_image(pdf, 2550, 3300))
            content = (f"q {w} 0 0 {h} 0 0 cm /Im1 Do Q\n"
                       + _page_content(num, total, label, w, h).split("\n", 1)[1])
        elif special == "form":
            content += _text(100, 230, 12, "Text field:")
            content += _text(125, 144, 12, "Checkbox (checked)")

        page.Resources = resources
        page.Contents = pdf.make_stream(content.encode("latin-1"))
        if rotate:
            page.Rotate = rotate
        if special == "form":
            _add_form_fields(pdf, page, fonts)

    with pdf.open_outline() as outline:
        for i, (label, *_rest) in enumerate(PAGES):
            outline.root.append(pikepdf.OutlineItem(f"{i + 1}. {label}", i))

    pdf.docinfo.Title = "SilverbowPDF test pages"
    pdf.save(path, encryption=encryption)


def build_long(path: Path, count: int = 300) -> None:
    pdf = pikepdf.Pdf.new()
    fonts = _fonts(pdf)
    for num in range(1, count + 1):
        pdf.add_blank_page(page_size=LETTER)
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=fonts)
        page.Contents = pdf.make_stream(
            _page_content(num, count, "Long document", *LETTER).encode("latin-1"))
    pdf.docinfo.Title = "SilverbowPDF long test"
    pdf.save(path)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    from test_annots import _make_annotated_pdf

    jobs = [
        ("test-pages.pdf", build_main, {}),
        ("test-protected.pdf", build_main,
         {"encryption": pikepdf.Encryption(user="test", owner="test-owner")}),
        ("test-long.pdf", build_long, {}),
        ("test-annotations.pdf", _make_annotated_pdf, {}),
    ]
    for name, build, kwargs in jobs:
        try:
            build(OUT / name, **kwargs)
            print(f"{OUT / name}  ({(OUT / name).stat().st_size / 1024:.0f} KB)")
        except PermissionError:
            # Windows won't replace a file another program has open.
            print(f"{OUT / name}  SKIPPED -- open in another program; close it and rerun")
        for leftover in OUT.glob(f".pikepdf.{name}*"):
            leftover.unlink(missing_ok=True)
