"""Page operations: the edit model, undo history, and saving.

The core guarantee is what-you-see-is-what-you-save: a page rendered in the
preview (original file + PageList) must be pixel-identical to the same page
rendered from the saved file. Everything else is bookkeeping around that.

Run with pytest, or directly:  python tests/test_pages.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pikepdf  # noqa: E402

from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.pagelist import History, PageList, PageRef  # noqa: E402
from pdfapp.writer import EncryptionWouldBeLost, SaveError, commit, write_pdf  # noqa: E402

# (width, height, intrinsic /Rotate, cropbox or None). Every page a different
# size, so page order is checkable from geometry alone.
PAGES = [
    (612, 792, 0, None),
    (792, 612, 90, None),
    (500, 700, 270, (40, 60, 460, 640)),
    (400, 400, 180, None),
    (300, 600, 0, (10, 20, 290, 580)),
]


def _make_pdf(path, encryption=None):
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
    for i, (w, h, rot, crop) in enumerate(PAGES):
        pdf.add_blank_page(page_size=(w, h))
        page = pdf.pages[-1]
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        # Asymmetric content: a wrong rotation or mirrored render shows up as
        # a pixel mismatch, not just a size mismatch.
        page.Contents = pdf.make_stream(
            f"0 0 1 RG 3 w 15 15 {w - 30} {h - 30} re S "
            f"1 0 0 rg 30 30 50 20 re f "
            f"BT /F1 24 Tf 40 {h - 60} Td (Page {i + 1}) Tj ET".encode()
        )
        if rot:
            page.Rotate = rot
        if crop:
            page.CropBox = pikepdf.Array(list(crop))
    pdf.save(path, encryption=encryption)


def _render(doc: PdfDocument, index: int, view_rotation: int = 0):
    bitmap = doc.render_bitmap(index, 0.5, view_rotation)
    try:
        return (bitmap.width, bitmap.height, bytes(bitmap.buffer))
    finally:
        bitmap.close()


# ---- PageList --------------------------------------------------------------

def test_pagelist_edits():
    p = PageList.identity(5)
    assert [r.source for r in p.deleted([1, 3])] == [0, 2, 4]
    assert [r.source for r in p.moved([3, 4], 0)] == [3, 4, 0, 1, 2]
    assert [r.source for r in p.moved([0], 4)] == [1, 2, 3, 4, 0]
    assert [r.source for r in p.moved([1, 3], 1)] == [0, 1, 3, 2, 4]
    r = p.rotated([0, 2], 90).rotated([2], 270)
    assert r[0] == PageRef(0, 90) and r[2] == PageRef(2, 0)
    assert p.rotated([1], -90)[1].rotation == 270
    assert p == PageList.identity(5), "edits must not mutate the original"
    assert r.rotated([0], -90).is_identity(5)


def test_pagelist_refuses_bad_edits():
    p = PageList.identity(3)
    for bad in (lambda: p.deleted([0, 1, 2]), lambda: p.deleted([]),
                lambda: p.rotated([0], 45), lambda: p.moved([], 0)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("should have raised ValueError")
    try:
        p.deleted([3])
    except IndexError:
        pass
    else:
        raise AssertionError("out-of-range index should raise")


# ---- History ---------------------------------------------------------------

def test_history_undo_redo_and_dirty():
    h = History("a")
    assert not h.dirty and not h.can_undo()
    h.push("b", "to b")
    h.push("c", "to c")
    assert h.dirty and h.undo_label() == "to c"
    assert h.undo() == "b" and h.redo_label() == "to c"
    h.mark_saved()
    assert not h.dirty
    assert h.redo() == "c" and h.dirty
    assert h.undo() == "b" and not h.dirty, "back at the saved state is clean"

    h.push("b", "no-op")  # identical state is not an edit
    assert h.redo_label() == "to c"

    h.undo()
    h.push("x", "branch")  # saved state 'b' is now unreachable by redo
    assert not h.can_redo()
    assert h.undo() == "a" and h.dirty
    assert h.redo() == "x" and h.dirty, "saved state was discarded, never clean again"


# ---- PdfDocument with edits ---------------------------------------------

def test_document_maps_displayed_to_source():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "in.pdf")
        _make_pdf(path)
        with PdfDocument(path) as doc:
            before = [doc.transform(i).display_size_pt for i in range(doc.page_count)]
            doc.pages = doc.pages.moved([4], 0).rotated([0], 90).deleted([2])
            assert doc.page_count == 4 and doc.source_page_count == 5
            w, h = before[4]
            assert doc.transform(0).display_size_pt == (h, w), "moved + rotated page"
            assert doc.transform(1).display_size_pt == before[0]
            assert doc.page_rotation(0) == 90


# ---- Saving ----------------------------------------------------------------

def test_saved_file_matches_preview():
    """The one that matters: preview pixels == saved-file pixels."""
    edits = (
        PageList.identity(len(PAGES))
        .rotated([1, 2], 90)
        .rotated([2], 90)
        .moved([4], 0)
        .deleted([3])
        .rotated([0], 270)
    )
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "in.pdf"), Path(tmp, "out.pdf")
        _make_pdf(src)

        with PdfDocument(src) as doc:
            doc.pages = edits
            preview = [[_render(doc, i, vr) for vr in (0, 90)] for i in range(doc.page_count)]
            commit(write_pdf(src, None, edits, out), out)

        with PdfDocument(out) as saved:
            assert saved.page_count == len(edits)
            for i in range(saved.page_count):
                for j, vr in enumerate((0, 90)):
                    got = _render(saved, i, vr)
                    assert got[:2] == preview[i][j][:2], (
                        f"page {i} view {vr}: saved {got[:2]} vs preview {preview[i][j][:2]}")
                    assert got[2] == preview[i][j][2], f"page {i} view {vr}: pixels differ"

        # Save-over-self, the ordinary Ctrl+S path.
        commit(write_pdf(src, None, edits, src), src)
        with PdfDocument(src) as doc:
            assert doc.page_count == len(edits)

        leftovers = [p.name for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
        assert not leftovers, f"temp files left behind: {leftovers}"


def test_encrypted_source_is_not_silently_decrypted():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "enc.pdf"), Path(tmp, "out.pdf")
        _make_pdf(src, encryption=pikepdf.Encryption(user="u", owner="o"))
        edits = PageList.identity(len(PAGES)).deleted([0])
        try:
            write_pdf(src, "u", edits, out)
        except EncryptionWouldBeLost:
            pass
        else:
            raise AssertionError("encrypted source saved without opt-in")
        assert not out.exists()
        assert not [p for p in Path(tmp).iterdir() if p.suffix == ".tmp"]

        commit(write_pdf(src, "u", edits, out, allow_decrypt=True), out)
        with PdfDocument(out) as doc:
            assert doc.page_count == len(PAGES) - 1


def test_failed_commit_leaves_original_intact():
    if sys.platform != "win32":
        return  # the lock that makes this fail is Windows-specific
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "in.pdf")
        _make_pdf(src)
        original = src.read_bytes()
        edits = PageList.identity(len(PAGES)).deleted([0])
        tmp_file = write_pdf(src, None, edits, src)
        holder = PdfDocument(src)  # pdfium holds the file open, as the app does
        _ = holder.page_box(0)
        try:
            commit(tmp_file, src)
        except SaveError:
            pass
        else:
            holder.close()
            return  # this pdfium build doesn't lock; nothing to test
        holder.close()
        assert src.read_bytes() == original
        assert not tmp_file.exists()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}\n      {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
