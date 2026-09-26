"""Annotation editing: the model, reading via pdfium, and writing via pikepdf.

The core guarantee again is what-you-see-is-what-you-save: a page previewed
with annotation edits (page content + overlay of edited annotations) must be
pixel-identical to the same page rendered from the saved file.

Run with pytest, or directly:  python tests/test_annots.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pikepdf  # noqa: E402
from pikepdf import Array, Dictionary, Name, String  # noqa: E402

from pdfapp.annotpdf import overlay_pdf  # noqa: E402
from pdfapp.annots import _quad_boxes  # noqa: E402
from pdfapp.annots import (  # noqa: E402
    HIGHLIGHT_COLORS, AnnotEdits, DocState, lay_markup, new_markup, new_note, without)
from pdfapp.document import PdfDocument  # noqa: E402
from pdfapp.pagelist import History, PageList  # noqa: E402
from pdfapp.writer import commit, write_pdf  # noqa: E402


def _ap(pdf, w, h, ops):
    s = pdf.make_stream(ops.encode())
    s.Type, s.Subtype = Name.XObject, Name.Form
    s.BBox = Array([0, 0, w, h])
    return s


def _make_annotated_pdf(path):
    """Three pages.

    Page 1: content, then /Annots =
        0 Link, 1 Text note (with popup), 2 its Popup, 3 Square, 4 Highlight,
        5 reply to the note (/IRT), 6 hidden Text.
    Page 2: a form field (NeedAppearances) and a Square.
    Page 3: /Rotate 90, one Text note.
    """
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    for i in range(3):
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=font, Helv=font))
        page.Contents = pdf.make_stream(
            f"0.9 0.95 1 rg 0 0 612 792 re f 0 0 1 RG 3 w 20 20 572 752 re S 0 g "
            f"BT /F1 28 Tf 72 700 Td (Annotated page {i + 1}) Tj ET "
            f"BT /F1 14 Tf 72 640 Td (The quick brown fox jumps over the lazy dog.) Tj ET"
            .encode())
    p1, p2, p3 = (pg.obj for pg in pdf.pages)
    p3.Rotate = 90

    def annot(page, **kw):
        kw.setdefault("F", 4)
        return pdf.make_indirect(Dictionary(Type=Name.Annot, P=page, **kw))

    link = annot(p1, Subtype=Name.Link, Rect=Array([72, 690, 300, 730]),
                 Dest=Array([p2, Name.Fit]), Border=Array([0, 0, 0]))
    note = annot(p1, Subtype=Name.Text, Rect=Array([400, 600, 420, 620]),
                 Contents=String("Original note"), T=String("alice"),
                 AP=Dictionary(N=_ap(pdf, 20, 20, "1 0.8 0 rg 0 0 20 20 re f")))
    popup = annot(p1, Subtype=Name.Popup, Rect=Array([420, 500, 580, 600]), Parent=note)
    note.Popup = popup
    square = annot(p1, Subtype=Name.Square, Rect=Array([100, 300, 250, 400]),
                   C=Array([1, 0, 0]),
                   AP=Dictionary(N=_ap(pdf, 150, 100, "1 0 0 RG 4 w 2 2 146 96 re S")))
    highlight = annot(p1, Subtype=Name.Highlight, Rect=Array([70, 635, 380, 660]),
                      QuadPoints=Array([70, 660, 380, 660, 70, 635, 380, 635]),
                      C=Array([1, 1, 0]), Contents=String("important"),
                      AP=Dictionary(N=_ap(pdf, 310, 25,
                                          "/GS0 gs 1 1 0 rg 0 0 310 25 re f")))
    highlight.AP.N.Resources = Dictionary(ExtGState=Dictionary(
        GS0=Dictionary(Type=Name.ExtGState, BM=Name.Multiply)))
    reply = annot(p1, Subtype=Name.Text, Rect=Array([430, 600, 450, 620]),
                  Contents=String("A reply"), IRT=note, T=String("bob"),
                  AP=Dictionary(N=_ap(pdf, 20, 20, "0 0.6 1 rg 0 0 20 20 re f")))
    hidden = annot(p1, Subtype=Name.Text, Rect=Array([10, 10, 30, 30]), F=2,
                   Contents=String("hidden"))
    p1.Annots = Array([link, note, popup, square, highlight, reply, hidden])

    field = annot(p2, Subtype=Name.Widget, FT=Name.Tx, T=String("name"),
                  V=String("Filled in"), Rect=Array([100, 500, 400, 530]),
                  DA=String("/Helv 14 Tf 0 g"))
    sq2 = annot(p2, Subtype=Name.Square, Rect=Array([300, 200, 400, 300]),
                AP=Dictionary(N=_ap(pdf, 100, 100, "0 0.6 0 RG 3 w 2 2 96 96 re S")))
    p2.Annots = Array([field, sq2])
    pdf.Root.AcroForm = Dictionary(Fields=Array([field]), NeedAppearances=True,
                                   DR=Dictionary(Font=Dictionary(Helv=font)),
                                   DA=String("/Helv 0 Tf 0 g"))

    note3 = annot(p3, Subtype=Name.Text, Rect=Array([100, 100, 120, 120]),
                  Contents=String("sideways page"),
                  AP=Dictionary(N=_ap(pdf, 20, 20, "0.5 0 0.5 rg 0 0 20 20 re f")))
    p3.Annots = Array([note3])
    pdf.save(path)


def _render(doc, index, view_rotation=0, overlay=None, scale=0.75):
    bitmap = doc.render_bitmap(index, scale, view_rotation, overlay)
    try:
        return bitmap.width, bitmap.height, bytes(bitmap.buffer)
    finally:
        bitmap.close()


def _overlay(doc, index):
    edits = doc.annots.get(doc.page_key(index))
    if edits is None:
        return None
    media, crop, rotate = doc.page_geometry(index)
    source = doc.sources[doc.pages[index].file]
    return overlay_pdf(source.path, source.password, doc.pages[index].source, edits,
                       media, crop, rotate)


def _differing_pixels(a, b) -> int:
    """Pixels whose channels differ by more than 2 (antialiasing noise)."""
    assert a[:2] == b[:2], f"size {a[:2]} vs {b[:2]}"
    da, db = a[2], b[2]
    bad = 0
    for i in range(0, len(da), 4):
        if max(abs(da[i + k] - db[i + k]) for k in range(3)) > 2:
            bad += 1
    return bad


# ---- model -----------------------------------------------------------------

def test_model():
    n = new_note(100, 200, "hi", "me")
    assert n.rect == (100, 180, 120, 200) and n.origin is None and n.ident[0] == "n"
    m = n.moved(5, -5).moved(1, 1)
    assert m.rect == (106, 176, 126, 196) and m.offset == (6, -4) and m.ident == n.ident
    e1 = AnnotEdits().with_page((0, 1), [n])
    e2 = AnnotEdits().with_page((0, 1), [n])
    assert e1 == e2 and hash(e1) == hash(e2) and e1 != AnnotEdits()
    h = History(DocState(PageList.identity(2)))
    h.push(DocState(PageList.identity(2), e1), "Add Note")
    h.push(DocState(PageList.identity(2), e2), "no-op")  # equal state: not an edit
    assert h.undo_label() == "Add Note"
    assert h.undo().annots == AnnotEdits()


# ---- reading ---------------------------------------------------------------

def test_reads_file_annotations():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            got = [(a.kind, a.origin, a.contents) for a in doc.file_annots(0)]
            assert got == [("Text", 1, "Original note"), ("Square", 3, ""),
                           ("Highlight", 4, "important"), ("Text", 5, "A reply")], got
            assert doc.file_annots(0)[0].rect == (400, 600, 420, 620)
            assert doc.file_annots(0)[0].author == "alice"
            assert [a.reply_to for a in doc.file_annots(0)] == [None, None, None, 1]
            assert without(doc.file_annots(0), doc.file_annots(0)[0]) == doc.file_annots(0)[1:3],                 "deleting a comment deletes its reply"
            assert [a.kind for a in doc.file_annots(1)] == ["Square"], "widget excluded"
            assert doc.page_annots(0) == doc.file_annots(0), "no edits yet"


# ---- preview == saved ----------------------------------------------------

def _edit_everything(doc):
    p1 = list(doc.file_annots(0))
    # delete the note (its popup and reply must go too), move the square,
    # edit the highlight's comment, add a note
    edited_p1 = list(without(p1, p1[0]))  # the note, and with it its reply
    edited_p1 = [a.moved(40, -60) if a.kind == "Square" else a for a in edited_p1]
    edited_p1 = [a.with_contents("changed") if a.kind == "Highlight" else a for a in edited_p1]
    edited_p1.append(new_note(500, 700, "new on page 1", "tester", (0.3, 0.8, 1.0)))
    p2 = list(doc.file_annots(1)) + [new_note(120, 480, "on the form page", "tester")]
    p3 = [a.moved(-30, 50) for a in doc.file_annots(2)] + [new_note(300, 400, "x", "t")]
    return (doc.annots.with_page(doc.page_key(0), edited_p1)
            .with_page(doc.page_key(1), p2)
            .with_page(doc.page_key(2), p3))


def test_preview_matches_saved():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            doc.annots = _edit_everything(doc)
            preview = {(i, vr): _render(doc, i, vr, _overlay(doc, i))
                       for i in range(3) for vr in (0, 90)}
            unedited = _render(doc, 0, 0, None)
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots), out)

        with PdfDocument(out) as saved:
            for (i, vr), want in preview.items():
                got = _render(saved, i, vr)
                bad = _differing_pixels(got, want)
                assert bad == 0, f"page {i + 1} view {vr}: {bad} pixels differ from preview"
            assert _render(saved, 0) != unedited, "edits must actually change the page"


def test_edits_land_in_the_file():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            doc.annots = _edit_everything(doc)
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots), out)

        with pikepdf.open(out) as pdf:
            annots = list(pdf.pages[0].obj.Annots)
            kinds = [str(a.Subtype) for a in annots]
            assert kinds == ["/Link", "/Square", "/Highlight", "/Text", "/Text"], kinds
            assert "/F" in annots[3] and int(annots[3].F) == 2, "hidden note passes through"
            assert [float(v) for v in annots[1].Rect] == [140, 240, 290, 340], "square moved"
            assert str(annots[2].Contents) == "changed" and "/M" in annots[2]
            new = annots[4]
            assert str(new.Contents) == "new on page 1" and str(new.T) == "tester"
            assert "/AP" in new and "/NM" in new and "/M" in new
            assert new.P.objgen == pdf.pages[0].obj.objgen
            # Nothing of the deleted note, its popup or its reply survives.
            texts = {str(o.get("/Contents", "")) for o in pdf.objects
                     if isinstance(o, Dictionary) and o.get("/Type") == Name.Annot}
            assert "Original note" not in texts and "A reply" not in texts
            assert not any(isinstance(o, Dictionary) and o.get("/Subtype") == Name.Popup
                           for o in pdf.objects), "orphaned popup"
            # rotated page: moved note's geometry
            assert [float(v) for v in pdf.pages[2].obj.Annots[0].Rect] == [70, 150, 90, 170]

        with PdfDocument(out) as saved:  # and it reads back as editable annotations
            assert [a.contents for a in saved.file_annots(0)] == ["changed", "new on page 1"] \
                or [a.kind for a in saved.file_annots(0)] == ["Square", "Highlight", "Text"]


def test_moving_a_highlight_moves_its_quads():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            edited = [a.moved(10, -100) if a.kind == "Highlight" else a
                      for a in doc.file_annots(0)]
            doc.annots = doc.annots.with_page(doc.page_key(0), edited)
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots), out)
        with pikepdf.open(out) as pdf:
            hl = next(a for a in pdf.pages[0].obj.Annots if a.Subtype == Name.Highlight)
            assert [float(v) for v in hl.QuadPoints] == [80, 560, 390, 560, 80, 535, 390, 535]


def test_annotations_follow_their_page():
    """Edits are keyed by page identity: reorder, delete, and merge them."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            f = doc.add_source(src)
            doc.pages = doc.pages.inserted(doc.source_refs(f)[2:3], 0).moved([3], 1).deleted([2])
            # displayed: [foreign p3, own p3, own p1]
            doc.annots = (doc.annots
                          .with_page(doc.page_key(0), [new_note(50, 50, "on the copy", "t")])
                          .with_page(doc.page_key(2), []))  # clear page 1's editables
            preview = [_render(doc, i, 0, _overlay(doc, i)) for i in range(doc.page_count)]
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots), out)
        with PdfDocument(out) as saved:
            assert [a.contents for a in saved.file_annots(0)] == ["on the copy"]
            assert [a.contents for a in saved.file_annots(1)] == ["sideways page"]
            assert saved.file_annots(2) == ()
            for i in range(saved.page_count):
                assert _differing_pixels(_render(saved, i), preview[i]) == 0, f"page {i + 1}"


# ---- text and text markup ----------------------------------------------------

def _find(doc, index, needle):
    text = doc.text_in(index, 0, doc.char_count(index) - 1)
    start = text.index(needle)
    return start, start + len(needle) - 1


def test_text_layer():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            start, end = _find(doc, 0, "quick brown")
            assert doc.text_in(0, end, start) == "quick brown", "either order"
            rects = doc.text_rects(0, start, end)
            assert len(rects) == 1, rects
            left, bottom, right, top = rects[0]
            assert 630 < bottom < top < 665 and 72 < left < right < 400, rects[0]
            assert start <= doc.char_at(0, (left + right) / 2, (bottom + top) / 2) <= end
            assert doc.char_at(0, 300, 100) == -1, "empty area"
            first, _ = _find(doc, 0, "The quick")
            assert doc.nearest_char(0, 40, 645) == first, "margin left of the line"
            assert doc.word_at(0, start + 2) == (start, start + 4)  # "quick"
            # Two lines: "Annotated page 1" through "The" -> one rect per line
            a, _ = _find(doc, 0, "Annotated")
            _, the = _find(doc, 0, "The")
            assert len(doc.text_rects(0, a, the)) == 2


def test_markup_preview_matches_saved():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            marks = {}
            for index, kind, needle in ((0, "Highlight", "quick brown"),
                                        (0, "Underline", "jumps over"),
                                        (0, "StrikeOut", "lazy"),
                                        (1, "Highlight", "Annotated page 2"),
                                        (2, "Highlight", "brown fox"),     # rotated page
                                        (2, "StrikeOut", "Annotated")):
                rects = doc.text_rects(index, *_find(doc, index, needle))
                marks.setdefault(index, []).append(new_markup(kind, rects, "tester"))
            # A highlight across two lines: two quads, one annotation.
            a, _ = _find(doc, 0, "Annotated")
            _, the = _find(doc, 0, "The")
            two = new_markup("Highlight", doc.text_rects(0, a, the), "tester",
                             HIGHLIGHT_COLORS["Green"])
            assert len(two.quads) == 16
            marks[0].append(two)
            edits = doc.annots
            for index, new in marks.items():
                edits = edits.with_page(doc.page_key(index), doc.file_annots(index) + tuple(new))
            doc.annots = edits
            preview = {(i, vr): _render(doc, i, vr, _overlay(doc, i))
                       for i in range(3) for vr in (0, 270)}
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots), out)

        with PdfDocument(out) as saved:
            for (i, vr), want in preview.items():
                bad = _differing_pixels(_render(saved, i, vr), want)
                assert bad == 0, f"page {i + 1} view {vr}: {bad} pixels differ from preview"
            assert [a.kind for a in saved.file_annots(0)][-4:] == \
                ["Highlight", "Underline", "StrikeOut", "Highlight"]
        with pikepdf.open(out) as pdf:
            hl = [a for a in pdf.pages[0].obj.Annots if a.Subtype == Name.Highlight][-1]
            assert len(hl.QuadPoints) == 16 and [float(c) for c in hl.C] == \
                list(HIGHLIGHT_COLORS["Green"])
            assert hl.AP.N.Resources.ExtGState.GS0.BM == Name.Multiply


def test_markup_text_stays_readable():
    """A highlight must not hide the text under it: pixels inside the
    highlighted word that were dark stay dark."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            rects = doc.text_rects(1, *_find(doc, 1, "Annotated page 2"))
            doc.annots = doc.annots.with_page(
                doc.page_key(1), doc.file_annots(1) + (new_markup("Highlight", rects, "t"),))
            w, h, plain = _render(doc, 1, 0, None, scale=1.0)
            _, _, marked = _render(doc, 1, 0, _overlay(doc, 1), scale=1.0)
            left, bottom, right, top = rects[0]
            dark = changed_dark = 0
            for y in range(int(792 - top) + 2, int(792 - bottom) - 2):
                for x in range(int(left) + 2, int(right) - 2):
                    i = (y * w + x) * 4
                    if max(plain[i:i + 3]) < 80:      # a text pixel
                        dark += 1
                        if max(marked[i:i + 3]) > 110:
                            changed_dark += 1
            assert dark > 50, "fixture: expected text under the highlight"
            assert changed_dark == 0, f"{changed_dark} text pixels washed out"


# ---- one layer per markup kind -------------------------------------------------

def _box(left, right, bottom=100.0, top=112.0):
    return (left, bottom, right, top)


def test_lay_markup_model():
    blue, green = HIGHLIGHT_COLORS["Blue"], HIGHLIGHT_COLORS["Green"]
    old = new_markup("Highlight", [_box(100, 200)], "a", blue).with_contents("keep me")
    under = new_markup("Underline", [_box(100, 200)], "a")

    # New mark inside the old one: old splits in two, keeps its comment.
    result = lay_markup((old, under), new_markup("Highlight", [_box(140, 160)], "b", green))
    split = next(a for a in result if a.ident == old.ident)
    assert [box for box, _ in _quad_boxes(split.quads)] \
        == [_box(100, 140), _box(160, 200)]
    assert split.contents == "keep me" and split.color == blue and split.rect == _box(100, 200)
    assert under in result, "a highlight never cuts an underline"

    # Covered completely: old goes, its comment moves to the new mark.
    result = lay_markup((old,), new_markup("Highlight", [_box(90, 210)], "b", green))
    assert [a.color for a in result] == [green] and result[0].contents == "keep me"

    # Other line: untouched.
    other_line = new_markup("Highlight", [_box(100, 200, 300, 312)], "b", green)
    assert lay_markup((old,), other_line)[0] == old

    # Same text again and again: always exactly one layer.
    marks = (old,)
    for _ in range(5):
        marks = lay_markup(marks, new_markup("Highlight", [_box(100, 200)], "a", blue))
    assert len(marks) == 1


def _page_render_with(doc, index, marks_for_page):
    doc.annots = AnnotEdits().with_page(doc.page_key(index), marks_for_page)
    return _render(doc, index, 0, _overlay(doc, index), scale=1.0)


def test_repeated_highlight_never_darkens():
    """The reported bug: highlighting the same words again must not stack."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            rects = doc.text_rects(1, *_find(doc, 1, "quick brown"))
            wider = doc.text_rects(1, *_find(doc, 1, "The quick brown fox"))
            base = doc.file_annots(1)
            once = _page_render_with(doc, 1, lay_markup(base, new_markup("Highlight", rects, "t")))
            marks = base
            for color in HIGHLIGHT_COLORS.values():   # every colour, over and over
                for r in (rects, wider, rects):
                    marks = lay_markup(marks, new_markup("Highlight", r, "t", color))
            marks = lay_markup(marks, new_markup("Highlight", rects, "t"))  # end on yellow
            # "quick brown" is now yellow, the rest of the wider span the last
            # colour -- compare only inside "quick brown".
            many = _page_render_with(doc, 1, marks)
            assert [a.kind for a in marks].count("Highlight") == 2, marks
            left, bottom, right, top = rects[0]
            w = once[0]
            for y in range(int(792 - top) + 1, int(792 - bottom) - 1):
                for x in range(int(left) + 1, int(right) - 1):
                    i = (y * w + x) * 4
                    assert once[2][i:i + 3] == many[2][i:i + 3], f"pixel {x},{y} darkened"


def test_new_colour_replaces_existing_file_highlight():
    """Page 1's file highlight is yellow across the sentence. Green over
    'brown fox' must show pure green there -- no yellow under it -- keep the
    old highlight (and its comment) on the rest, and save as previewed."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)
        with PdfDocument(src) as doc:
            rects = doc.text_rects(0, *_find(doc, 0, "brown fox"))
            green = new_markup("Highlight", rects, "t", HIGHLIGHT_COLORS["Green"])
            recoloured = _page_render_with(doc, 0, lay_markup(doc.file_annots(0), green))
            reference = _page_render_with(  # the old highlight gone entirely, green alone
                doc, 0, tuple(a for a in doc.file_annots(0) if a.kind != "Highlight") + (green,))
            left, bottom, right, top = rects[0]
            w = recoloured[0]
            for y in range(int(792 - top) + 1, int(792 - bottom) - 1):
                for x in range(int(left) + 1, int(right) - 1):
                    i = (y * w + x) * 4
                    assert recoloured[2][i:i + 3] == reference[2][i:i + 3], f"yellow under green at {x},{y}"

            doc.annots = AnnotEdits().with_page(doc.page_key(0), lay_markup(doc.file_annots(0), green))
            preview = _render(doc, 0, 0, _overlay(doc, 0))
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots), out)
        with PdfDocument(out) as saved:
            assert _differing_pixels(_render(saved, 0), preview) == 0
            highlights = [a for a in saved.file_annots(0) if a.kind == "Highlight"]
            assert len(highlights) == 2
            assert highlights[0].contents == "important" and len(highlights[0].quads) == 16, \
                "old highlight kept, split around the new one, comment intact"


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
