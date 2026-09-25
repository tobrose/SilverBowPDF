"""Validate PageTransform against pdfium's own coordinate converter.

The whole point of PageTransform is that its math is right. Hand-checking
rotation arithmetic is how you end up shipping an annotation tool that puts
the highlight in the wrong corner on landscape scans. So instead of trusting
the derivation, every conversion is checked against pdfium's PdfPosConv,
which is the same code path that actually rasterises the page.

Run with pytest, or directly:  python tests/test_transform.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pikepdf  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402

from pdfapp.transform import PageTransform, normalize_rotation, normalize_rect  # noqa: E402

TOL = 1.0  # pixels; pdfium's posconv rounds to int, so allow a pixel of slop


def _make_pdf(path, media=(0, 0, 612, 792), crop=None, rotate=0):
    """Build a one-page PDF with a specific box and /Rotate."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(media[2] - media[0], media[3] - media[1]))
    page.MediaBox = pikepdf.Array([media[0], media[1], media[2], media[3]])
    if crop is not None:
        page.CropBox = pikepdf.Array(list(crop))
    if rotate:
        page.Rotate = rotate
    pdf.save(path)


def _grid(box, n=5):
    x0, y0, x1, y1 = box
    for i in range(n):
        for j in range(n):
            yield (x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * j / (n - 1))


def test_against_pdfium_posconv():
    """PageTransform.pdf_to_bitmap must agree with pdfium for every case."""
    cases = [
        # (mediabox, cropbox, intrinsic /Rotate, view rotation, scale)
        ((0, 0, 612, 792), None, 0, 0, 1.0),
        ((0, 0, 612, 792), None, 0, 90, 2.0),
        ((0, 0, 612, 792), None, 90, 0, 1.5),
        ((0, 0, 612, 792), None, 180, 90, 1.0),
        ((0, 0, 612, 792), None, 270, 180, 0.75),
        ((0, 0, 612, 792), None, 90, 270, 3.0),
        ((0, 0, 842, 595), None, 0, 270, 1.25),
        # Non-origin and offset crop boxes -- the case people forget.
        ((0, 0, 612, 792), (36, 48, 576, 744), 0, 0, 2.0),
        ((0, 0, 612, 792), (36, 48, 576, 744), 90, 90, 1.5),
        ((-100, -50, 512, 742), None, 0, 0, 1.0),
        ((-100, -50, 512, 742), None, 270, 90, 2.0),
    ]

    checked = 0
    with tempfile.TemporaryDirectory() as tmp:
        for idx, (media, crop, intrinsic, view, scale) in enumerate(cases):
            path = os.path.join(tmp, f"case{idx}.pdf")
            _make_pdf(path, media=media, crop=crop, rotate=intrinsic)

            doc = pdfium.PdfDocument(path)
            page = doc[0]
            box = tuple(page.get_cropbox())
            bitmap = page.render(scale=scale, rotation=view)
            posconv = bitmap.get_posconv(page)

            t = PageTransform(box=box, rotation=intrinsic + view, scale=scale)

            # Bitmap dimensions must match what pdfium actually allocated.
            assert t.bitmap_size == (bitmap.width, bitmap.height), (
                f"case {idx}: size mismatch, ours={t.bitmap_size} "
                f"pdfium=({bitmap.width}, {bitmap.height})"
            )

            for (x, y) in _grid(box):
                ours = t.pdf_to_bitmap(x, y)
                theirs = posconv.to_bitmap(x, y)
                assert abs(ours[0] - theirs[0]) <= TOL and abs(ours[1] - theirs[1]) <= TOL, (
                    f"case {idx} media={media} crop={crop} rot={intrinsic}+{view} "
                    f"scale={scale}: pdf({x:.1f}, {y:.1f}) -> ours={ours} theirs={theirs}"
                )
                checked += 1

            bitmap.close()
            doc.close()

    print(f"  checked {checked} points against pdfium across {len(cases)} configurations")


def test_roundtrip_pdf_bitmap_widget():
    """Every conversion must invert cleanly."""
    for rot in (0, 90, 180, 270):
        for scale in (0.5, 1.0, 2.75):
            t = PageTransform(
                box=(-20.0, 13.5, 592.0, 805.5), rotation=rot, scale=scale, offset=(37.0, -11.0)
            )
            for (x, y) in _grid(t.box, n=4):
                bx, by = t.pdf_to_bitmap(x, y)
                rx, ry = t.bitmap_to_pdf(bx, by)
                assert math.isclose(rx, x, abs_tol=1e-6), f"{rot}/{scale}: x {x} -> {rx}"
                assert math.isclose(ry, y, abs_tol=1e-6), f"{rot}/{scale}: y {y} -> {ry}"

                wx, wy = t.pdf_to_widget(x, y)
                px, py = t.widget_to_pdf(wx, wy)
                assert math.isclose(px, x, abs_tol=1e-6)
                assert math.isclose(py, y, abs_tol=1e-6)


def test_corners_land_where_expected():
    """Sanity anchors a human can read, independent of the derivation."""
    box = (0.0, 0.0, 100.0, 200.0)

    t = PageTransform(box, rotation=0, scale=1.0)
    assert t.pdf_to_bitmap(0, 200) == (0.0, 0.0), "unrotated: PDF top-left is bitmap origin"
    assert t.pdf_to_bitmap(0, 0) == (0.0, 200.0), "unrotated: PDF origin is bottom-left"
    assert t.bitmap_size == (100, 200)

    # 90 CW: the page's top-left corner swings round to the bitmap's top-right.
    t90 = PageTransform(box, rotation=90, scale=1.0)
    assert t90.bitmap_size == (200, 100)
    assert t90.pdf_to_bitmap(0, 200) == (200.0, 0.0)
    assert t90.pdf_to_bitmap(0, 0) == (0.0, 0.0)

    t180 = PageTransform(box, rotation=180, scale=1.0)
    assert t180.pdf_to_bitmap(0, 200) == (100.0, 200.0)

    t270 = PageTransform(box, rotation=270, scale=1.0)
    assert t270.bitmap_size == (200, 100)
    assert t270.pdf_to_bitmap(0, 200) == (0.0, 100.0)


def test_rect_normalisation_survives_rotation():
    """A rect must stay a sane left/top/right/bottom rect after rotating."""
    box = (0.0, 0.0, 612.0, 792.0)
    pdf_rect = (100.0, 600.0, 300.0, 700.0)  # 200x100 in PDF space
    for rot in (0, 90, 180, 270):
        t = PageTransform(box, rotation=rot, scale=2.0, offset=(10.0, 20.0))
        left, top, right, bottom = t.pdf_rect_to_widget(pdf_rect)
        assert left < right and top < bottom, f"rot {rot}: rect not normalised"

        w, h = right - left, bottom - top
        expected = (400.0, 200.0) if rot in (0, 180) else (200.0, 400.0)
        assert math.isclose(w, expected[0], abs_tol=1e-6), f"rot {rot}: width {w}"
        assert math.isclose(h, expected[1], abs_tol=1e-6), f"rot {rot}: height {h}"

        back = t.widget_rect_to_pdf((left, top, right, bottom))
        for a, b in zip(back, normalize_rect(pdf_rect)):
            assert math.isclose(a, b, abs_tol=1e-6), f"rot {rot}: roundtrip {back}"


def test_rotation_normalisation():
    assert normalize_rotation(-90) == 270
    assert normalize_rotation(450) == 90
    assert normalize_rotation(720) == 0
    assert normalize_rotation(-360) == 0
    for bad in (45, 1, -30):
        try:
            normalize_rotation(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} should have been rejected")


def test_rejects_garbage():
    for bad_kwargs in (
        dict(box=(0, 0, 0, 100)),      # zero width
        dict(box=(0, 0, 100, 100), scale=0),
        dict(box=(0, 0, 100, 100), scale=-2),
    ):
        try:
            PageTransform(**bad_kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"should have rejected {bad_kwargs}")

    # Reversed boxes get fixed rather than rejected -- real PDFs contain them.
    t = PageTransform(box=(612, 792, 0, 0))
    assert t.box == (0.0, 0.0, 612.0, 792.0)


def test_fit_helpers():
    t = PageTransform((0, 0, 612, 792))
    assert math.isclose(t.scale_to_fit_width(1224), 2.0)
    assert math.isclose(t.scale_to_fit_page(1224, 792), 1.0)  # height binds
    t90 = t.with_rotation(90)
    assert math.isclose(t90.scale_to_fit_width(792), 1.0)  # now 792 wide


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
