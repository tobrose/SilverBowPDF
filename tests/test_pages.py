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

from pdfapp.document import PdfDocument, SourceInfo  # noqa: E402
from pdfapp.pagelist import History, PageList, PageRef  # noqa: E402
from pdfapp.security import REMOVE  # noqa: E402
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


def test_drop_position():
    p = PageList.identity(6)

    def drop(sel, before):
        return [r.source for r in p.moved(sel, p.insert_position(sel, before))]

    assert drop([0], 3) == [1, 2, 0, 3, 4, 5], "drag first page to before row 3"
    assert drop([4], 1) == [0, 4, 1, 2, 3, 5], "drag upward"
    assert drop([2], 6) == [0, 1, 3, 4, 5, 2], "drop past the end"
    assert drop([1, 2], 2) == list(range(6)), "block dropped onto itself"
    assert drop([1, 2], 3) == list(range(6)), "block dropped just below itself"
    assert drop([1, 4], 0) == [1, 4, 0, 2, 3, 5], "scattered pages gather at the drop"
    assert drop([1, 4], 6) == [0, 2, 3, 5, 1, 4]


def test_closed_document_fails_loudly():
    """pdfium on a closed document is an access violation. It must be a
    catchable error instead."""
    from pdfapp.document import DocumentError
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "in.pdf")
        _make_pdf(path)
        doc = PdfDocument(path)
        doc.close()
        for call in (lambda: doc.transform(0), lambda: doc.render_bitmap(0, 1.0),
                     lambda: doc.char_count(0), lambda: doc.file_annots(0)):
            try:
                call()
            except DocumentError:
                pass
            else:
                raise AssertionError("closed document did not raise")


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
            commit(write_pdf([SourceInfo(src, None)], edits, out), out)

        with PdfDocument(out) as saved:
            assert saved.page_count == len(edits)
            for i in range(saved.page_count):
                for j, vr in enumerate((0, 90)):
                    got = _render(saved, i, vr)
                    assert got[:2] == preview[i][j][:2], (
                        f"page {i} view {vr}: saved {got[:2]} vs preview {preview[i][j][:2]}")
                    assert got[2] == preview[i][j][2], f"page {i} view {vr}: pixels differ"

        # Save-over-self, the ordinary Ctrl+S path.
        commit(write_pdf([SourceInfo(src, None)], edits, src), src)
        with PdfDocument(src) as doc:
            assert doc.page_count == len(edits)

        leftovers = [p.name for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
        assert not leftovers, f"temp files left behind: {leftovers}"


def _page_objects_in_file(path) -> int:
    with pikepdf.open(path) as pdf:
        return sum(1 for o in pdf.objects
                   if isinstance(o, pikepdf.Dictionary) and o.get("/Type") == pikepdf.Name.Page)


def _make_referencing_pdf(path):
    """Five pages, and every kind of reference to page 4 (index 3)."""
    from pikepdf import Array, Dictionary, Name, OutlineItem, String
    pdf = pikepdf.Pdf.new()
    for _ in range(5):
        pdf.add_blank_page()
    p = [pg.obj for pg in pdf.pages]

    with pdf.open_outline() as outline:
        for i in range(5):
            outline.root.append(OutlineItem(f"page {i + 1}", i))
        folder = OutlineItem("folder -> page 4", 3)       # dead, but has a live child
        folder.children.append(OutlineItem("child -> page 1", 0))
        folder.children.append(OutlineItem("child -> page 4", 3))
        outline.root.append(folder)
        outline.root.append(OutlineItem("named -> page 4", Name("/Four")))

    pdf.Root.Dests = Dictionary(Four=Array([p[3], Name.Fit]), One=Array([p[0], Name.Fit]))
    pdf.Root.Names = Dictionary(Dests=Dictionary(Names=Array([
        String("four"), Array([p[3], Name.Fit]), String("two"), Array([p[1], Name.Fit]),
    ])))

    def link(to):
        return pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Link,
                                            Rect=Array([0, 0, 50, 50]),
                                            Dest=Array([p[to], Name.Fit])))

    def widget(page, parent=None, **kw):
        d = Dictionary(Type=Name.Annot, Subtype=Name.Widget, Rect=Array([0, 0, 50, 20]),
                       P=p[page], **kw)
        if parent is not None:
            d.Parent = parent
        return pdf.make_indirect(d)

    only_on_4 = widget(3, FT=Name.Tx, T=String("only_on_4"))
    shared = pdf.make_indirect(Dictionary(FT=Name.Tx, T=String("shared")))
    shared.Kids = Array([widget(0, shared), widget(3, shared)])
    p[0].Annots = Array([link(3), link(1), shared.Kids[0]])
    p[3].Annots = Array([only_on_4, shared.Kids[1], link(0)])
    pdf.Root.AcroForm = Dictionary(Fields=Array([only_on_4, shared]))
    pdf.Root.OpenAction = Array([p[3], Name.Fit])
    pdf.save(path)


def test_removed_pages_leave_nothing_behind():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "refs.pdf"), Path(tmp, "out.pdf")
        _make_referencing_pdf(src)

        commit(write_pdf([SourceInfo(src, None)], PageList.identity(5).deleted([3]), out), out)
        assert _page_objects_in_file(out) == 4, "removed page still stored in the file"

        with pikepdf.open(out) as pdf:
            with pdf.open_outline() as outline:
                tree = [(i.title, [c.title for c in i.children]) for i in outline.root]
            assert tree == [("page 1", []), ("page 2", []), ("page 3", []), ("page 5", []),
                            ("folder -> page 4", ["child -> page 1"])], tree
            folder = pdf.Root.Outlines.First
            while str(folder.Title) != "folder -> page 4":
                folder = folder.Next
            assert "/Dest" not in folder, "folder kept, but its dead target must go"

            assert set(pdf.Root.Dests.keys()) == {"/One"}
            assert list(pikepdf.NameTree(pdf.Root.Names.Dests).keys()) == ["two"]

            annots = pdf.pages[0].obj.Annots
            links = [a for a in annots if a.Subtype == pikepdf.Name.Link]
            assert len(links) == 1 and links[0].Dest[0].objgen == pdf.pages[1].obj.objgen

            fields = pdf.Root.AcroForm.Fields
            assert [str(f.T) for f in fields] == ["shared"]
            assert len(fields[0].Kids) == 1
            assert "/OpenAction" not in pdf.Root

        with PdfDocument(out) as doc:  # and pdfium still likes it
            assert doc.page_count == 4
            _render(doc, 0)


def test_nothing_pruned_when_nothing_removed():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "refs.pdf"), Path(tmp, "out.pdf")
        _make_referencing_pdf(src)
        commit(write_pdf([SourceInfo(src, None)], PageList.identity(5).moved([4], 0), out), out)
        with pikepdf.open(out) as pdf:
            with pdf.open_outline() as outline:
                assert len(outline.root) == 7
            assert len(pdf.Root.AcroForm.Fields) == 2
            assert len(pdf.Root.Dests.keys()) == 2
            assert "/OpenAction" in pdf.Root


def test_inherited_page_attributes_survive_reorder():
    """Size, rotation and fonts can live on a parent /Pages node instead of
    the page. Reordering rebuilds the tree, so they must be pushed down first."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "inherit.pdf"), Path(tmp, "out.pdf")
        _make_pdf(src)
        with pikepdf.open(src, allow_overwriting_input=True) as pdf:
            root = pdf.Root.Pages
            first = pdf.pages[0].obj
            # Hoist page 1's attributes to the root, and make pages 1 and 3
            # rely on inheriting them.
            root.MediaBox = first.MediaBox
            root.Resources = first.Resources
            root.Rotate = 90
            for i in (0, 2):
                page = pdf.pages[i].obj
                for key in ("/MediaBox", "/Resources", "/Rotate", "/CropBox"):
                    if key in page:
                        del page[key]
            pdf.save(src)

        edits = PageList.identity(len(PAGES)).moved([0], 4).deleted([1])
        with PdfDocument(src) as doc:
            assert doc.page_rotation(0) == 90, "fixture: page 1 should inherit /Rotate"
            doc.pages = edits
            preview = [_render(doc, i) for i in range(doc.page_count)]
        commit(write_pdf([SourceInfo(src, None)], edits, out), out)
        with PdfDocument(out) as saved:
            assert [_render(saved, i) for i in range(saved.page_count)] == preview


def test_pagelist_insert():
    p = PageList.identity(3)
    ins = [PageRef(1, 0, 1), PageRef(0, 90, 1)]
    assert list(p.inserted(ins, 1)) == [PageRef(0), *ins, PageRef(1), PageRef(2)]
    assert list(p.inserted(ins, 99))[-2:] == ins
    assert p.rotated([0], 90).inserted(ins, 0).rotated([1], 90)[1] == PageRef(0, 180, 1)
    for bad in ([], [PageRef(2, 90)]):  # empty; page already present (any rotation)
        try:
            p.inserted(bad, 0)
        except ValueError:
            pass
        else:
            raise AssertionError(f"inserting {bad} should fail")


def test_merged_file_matches_preview():
    """Pages from three sources -- including the document inserted into
    itself -- reordered, rotated and trimmed, then saved: pixel-identical."""
    with tempfile.TemporaryDirectory() as tmp:
        base, other, out = Path(tmp, "base.pdf"), Path(tmp, "other.pdf"), Path(tmp, "out.pdf")
        _make_pdf(base)
        _make_referencing_pdf(other)
        with PdfDocument(base) as doc:
            f_other = doc.add_source(other)
            f_self = doc.add_source(base)
            edits = (doc.pages
                     .inserted([doc.source_refs(f_other)[i] for i in (3, 0)], 2)
                     .inserted(doc.source_refs(f_self)[1:3], 0)
                     .rotated([0, 4], 90)
                     .deleted([len(PAGES) + 3]))
            doc.pages = edits
            preview = [_render(doc, i, vr) for i in range(doc.page_count) for vr in (0, 90)]
            commit(write_pdf(doc.sources, edits, out), out)

        with PdfDocument(out) as saved:
            assert saved.page_count == len(edits)
            got = [_render(saved, i, vr) for i in range(saved.page_count) for vr in (0, 90)]
            for n, (a, b) in enumerate(zip(got, preview)):
                assert a[:2] == b[:2], f"render {n}: size {a[:2]} vs preview {b[:2]}"
                assert a[2] == b[2], f"render {n}: pixels differ"
        assert _page_objects_in_file(out) == len(edits), "orphaned page objects"


def test_merge_carries_forms_links_and_bookmarks():
    with tempfile.TemporaryDirectory() as tmp:
        base, other, out = Path(tmp, "base.pdf"), Path(tmp, "other.pdf"), Path(tmp, "out.pdf")
        _make_referencing_pdf(base)
        _make_referencing_pdf(other)   # same field names: must not collide
        with PdfDocument(base) as doc:
            f = doc.add_source(other)
            # Pages 1 and 4 of other: the "shared" field has widgets on both,
            # "only_on_4" on page 4. Page 1's links point at page 4 (copied)
            # and page 2 (not copied).
            edits = doc.pages.inserted([doc.source_refs(f)[0], doc.source_refs(f)[3]], 5)
            commit(write_pdf(doc.sources, edits, out), out)

        assert _page_objects_in_file(out) == 7, "an uncopied page came along"
        with pikepdf.open(out) as pdf:
            fields = {str(fld.T): fld for fld in pdf.Root.AcroForm.Fields}
            assert sorted(fields) == ["only_on_4", "only_on_4+1", "shared", "shared+1"], sorted(fields)

            kept = {pg.obj.objgen for pg in pdf.pages}
            # The imported copy of "shared" is still ONE field whose widgets sit
            # on both copied pages, so the two still share a value.
            on = {pg.obj.objgen: i for i, pg in enumerate(pdf.pages)}
            assert sorted(on[k.P.objgen] for k in fields["shared+1"].Kids) == [5, 6]
            assert all(k.Parent.objgen == fields["shared+1"].objgen for k in fields["shared+1"].Kids)
            copied_p1 = pdf.pages[5].obj
            links = [a for a in copied_p1.Annots if a.Subtype == pikepdf.Name.Link]
            assert len(links) == 1, "link to the uncopied page should be gone"
            assert links[0].Dest[0].objgen == pdf.pages[6].obj.objgen, "link should follow its page"

            with pdf.open_outline() as outline:
                folder = outline.root[-1]
                assert folder.title == "other.pdf", folder.title
                titles = [(c.title, [g.title for g in c.children]) for c in folder.children]
            assert titles == [
                ("page 1", []), ("page 4", []),
                ("folder -> page 4", ["child -> page 1", "child -> page 4"]),
                ("named -> page 4", []),
            ], titles
            for node in _walk_outline(pdf.Root.Outlines.First):
                if "/Dest" in node and isinstance(node.Dest, pikepdf.Array):
                    assert node.Dest[0].objgen in kept, f"{node.Title} points outside the document"

        with PdfDocument(out) as saved:  # pdfium is happy with the merged forms
            assert saved.page_count == 7
            _render(saved, 5)


def _walk_outline(node):
    seen = set()
    while isinstance(node, pikepdf.Dictionary) and node.objgen not in seen:
        seen.add(node.objgen)
        yield node
        yield from _walk_outline(node.get("/First"))
        node = node.get("/Next")


def test_inserting_from_protected_file_needs_consent():
    with tempfile.TemporaryDirectory() as tmp:
        base, prot, out = Path(tmp, "base.pdf"), Path(tmp, "prot.pdf"), Path(tmp, "out.pdf")
        _make_pdf(base)
        _make_pdf(prot, encryption=pikepdf.Encryption(user="u", owner="o"))
        with PdfDocument(base) as doc:
            f = doc.add_source(prot, "u")
            edits = doc.pages.inserted(doc.source_refs(f)[:1], 0)
            try:
                write_pdf(doc.sources, edits, out)
            except EncryptionWouldBeLost as exc:
                assert exc.file == f and exc.path.name == "prot.pdf"
            else:
                raise AssertionError("protected pages saved without consent")
            assert not out.exists()
            commit(write_pdf(doc.sources, edits, out, allow_decrypt={f}), out)
            # If the result is protected anyway, nothing is being lost: no ask.
            protected = Path(tmp, "protected.pdf")
            commit(write_pdf(doc.sources, edits, protected, protection="new"), protected)
        with pikepdf.open(out) as pdf:
            assert len(pdf.pages) == len(PAGES) + 1 and not pdf.is_encrypted
        with pikepdf.open(protected, password="new") as pdf:
            assert len(pdf.pages) == len(PAGES) + 1 and pdf.is_encrypted


def test_encrypted_document_keeps_its_protection():
    """Rule 5: saving never strips protection unless removing it was asked
    for. The default keeps it exactly -- passwords and restrictions -- even
    when the file was opened with the user password, not the owner's."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "enc.pdf")
        no_copy = pikepdf.Permissions(extract=False)
        _make_pdf(src, encryption=pikepdf.Encryption(user="u", owner="o", allow=no_copy))
        edits = PageList.identity(len(PAGES)).deleted([0])

        kept = Path(tmp, "kept.pdf")
        commit(write_pdf([SourceInfo(src, "u")], edits, kept), kept)
        try:
            pikepdf.open(kept).close()
        except pikepdf.PasswordError:
            pass
        else:
            raise AssertionError("protection was stripped")
        with pikepdf.open(kept, password="u") as pdf:
            assert len(pdf.pages) == len(PAGES) - 1 and not pdf.allow.extract
        with pikepdf.open(kept, password="o") as pdf:
            assert pdf.owner_password_matched, "the owner password still works"

        changed = Path(tmp, "changed.pdf")
        commit(write_pdf([SourceInfo(src, "o")], edits, changed, protection="fresh"), changed)
        with pikepdf.open(changed, password="fresh") as pdf:
            assert pdf.is_encrypted and pdf.encryption.bits == 256 and pdf.allow.extract
        for old in ("u", "o"):
            try:
                pikepdf.open(changed, password=old).close()
            except pikepdf.PasswordError:
                pass
            else:
                raise AssertionError(f"old password {old!r} still opens it")

        removed = Path(tmp, "removed.pdf")
        commit(write_pdf([SourceInfo(src, "o")], edits, removed, protection=REMOVE), removed)
        with PdfDocument(removed) as doc:  # no password
            assert doc.page_count == len(PAGES) - 1 and not doc.is_encrypted


def test_failed_commit_leaves_original_intact():
    if sys.platform != "win32":
        return  # the lock that makes this fail is Windows-specific
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "in.pdf")
        _make_pdf(src)
        original = src.read_bytes()
        edits = PageList.identity(len(PAGES)).deleted([0])
        tmp_file = write_pdf([SourceInfo(src, None)], edits, src)
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
