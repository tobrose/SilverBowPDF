"""Form filling: reading fields, writing values, and preview == saved.

Run with pytest, or directly:  python tests/test_forms.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pikepdf  # noqa: E402
from pikepdf import Array, Dictionary, Name, String  # noqa: E402

from pdfapp.document import PdfDocument  # noqa: E402
from dataclasses import replace  # noqa: E402

from pdfapp.fields import (  # noqa: E402
    CHECKBOX, COMBO, RADIO, TEXT, FieldEdits, FormEdits, is_on, new_field)
from pdfapp.writer import commit, write_pdf  # noqa: E402
from test_annots import _differing_pixels, _render, _shown  # noqa: E402


def _stream(pdf, w, h, ops):
    s = pdf.make_stream(ops.encode())
    s.Type, s.Subtype, s.BBox = Name.XObject, Name.Form, Array([0, 0, w, h])
    return s


def _make_form(path, need_appearances=False):
    """Page 1: name (bordered, blue bg), notes (multiline), agree (checkbox),
    size (radio S/M/L), colour (combo), id (read-only), person.name (nested).
    Page 2: a radio group whose kids use /Opt export values."""
    pdf = pikepdf.Pdf.new()
    helv = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding))
    zadb = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.ZapfDingbats))
    for _ in range(2):
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.Resources = Dictionary(Font=Dictionary(F1=helv))
        page.Contents = pdf.make_stream(
            b"BT /F1 12 Tf 72 740 Td (Application form) Tj ET "
            b"BT /F1 10 Tf 72 700 Td (Name:) Tj ET BT /F1 10 Tf 72 640 Td (Notes:) Tj ET")
    p1, p2 = (pg.obj for pg in pdf.pages)
    fields, annots1, annots2 = [], [], []

    def widget(page, rect, **kw):
        kw.setdefault("F", 4)
        return pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget,
                                            Rect=Array(rect), P=page, **kw))

    def check_ap(state, w=14, h=14):
        on = _stream(pdf, w, h, "0 0 0 rg BT /ZaDb 12 Tf 1 2 Td (4) Tj ET")
        on.Resources = Dictionary(Font=Dictionary(ZaDb=zadb))
        off = _stream(pdf, w, h, "")
        return Dictionary(N=Dictionary({"/" + state: on, "/Off": off}))

    name = widget(p1, [120, 690, 400, 712], FT=Name.Tx, T=String("name"), V=String(""),
                  DA=String("/Helv 0 Tf 0 g"),
                  MK=Dictionary(BG=Array([0.9, 0.95, 1.0]), BC=Array([0, 0, 0.6])),
                  BS=Dictionary(W=1))
    notes = widget(p1, [120, 560, 400, 650], FT=Name.Tx, T=String("notes"), Ff=4096,
                   DA=String("/Helv 10 Tf 0 0 0.6 rg"))
    agree = widget(p1, [72, 520, 86, 534], FT=Name.Btn, T=String("agree"), V=Name.Off,
                   AS=Name.Off, AP=check_ap("Yes"), MK=Dictionary(BC=Array([0])))
    size = pdf.make_indirect(Dictionary(FT=Name.Btn, Ff=(1 << 15) | (1 << 14), T=String("size"),
                                        V=Name.Off))
    size.Kids = Array([widget(p1, [72 + 40 * i, 480, 86 + 40 * i, 494], Parent=size, AS=Name.Off,
                              AP=check_ap(s)) for i, s in enumerate(("S", "M", "L"))])
    colour = widget(p1, [120, 440, 260, 460], FT=Name.Ch, Ff=1 << 17, T=String("colour"),
                    Opt=Array([String("Red"), String("Green"), String("Blue")]),
                    V=String("Red"), DA=String("/Helv 11 Tf 0 g"))
    ident = widget(p1, [300, 440, 400, 460], FT=Name.Tx, Ff=1, T=String("id"),
                   V=String("A-1"), DA=String("/Helv 11 Tf 0 g"))
    person = pdf.make_indirect(Dictionary(T=String("person")))
    pname = widget(p1, [120, 400, 400, 420], FT=Name.Tx, T=String("name"), Parent=person,
                   DA=String("/Helv 11 Tf 0 g"), Q=1)
    person.Kids = Array([pname])
    fields += [name, notes, agree, size, colour, ident, person]
    annots1 += [name, notes, agree, *size.Kids, colour, ident, pname]

    pick = pdf.make_indirect(Dictionary(FT=Name.Btn, Ff=(1 << 15) | (1 << 14), T=String("pick"),
                                        V=Name.Off, Opt=Array([String("first"), String("second")])))
    pick.Kids = Array([widget(p2, [72 + 40 * i, 700, 86 + 40 * i, 714], Parent=pick, AS=Name.Off,
                              AP=check_ap(str(i))) for i in range(2)])
    fields.append(pick)
    annots2 += list(pick.Kids)

    p1.Annots, p2.Annots = Array(annots1), Array(annots2)
    pdf.Root.AcroForm = Dictionary(Fields=Array(fields), DA=String("/Helv 0 Tf 0 g"),
                                   DR=Dictionary(Font=Dictionary(Helv=helv, ZaDb=zadb)))
    if need_appearances:
        pdf.Root.AcroForm.NeedAppearances = True
    pdf.save(path)


def _widget(doc, index, name, on_state=None):
    return next(w for w in doc.page_widgets(index)
                if w.name == name and (on_state is None or w.on_state == on_state))


def test_reads_fields():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "form.pdf")
        _make_form(src)
        with PdfDocument(src) as doc:
            kinds = {w.name: w.kind for w in doc.page_widgets(0)}
            assert kinds == {"name": TEXT, "notes": TEXT, "agree": CHECKBOX, "size": RADIO,
                             "colour": COMBO, "id": TEXT, "person.name": TEXT}, kinds
            assert _widget(doc, 0, "notes").multiline and not _widget(doc, 0, "name").multiline
            assert not _widget(doc, 0, "id").fillable, "read-only"
            assert _widget(doc, 0, "colour").options == ("Red", "Green", "Blue")
            assert _widget(doc, 0, "colour").value == "Red"
            assert sorted(w.on_state for w in doc.page_widgets(0) if w.name == "size") == ["L", "M", "S"]
            assert _widget(doc, 0, "agree").value == "Off"


def _fill(doc):
    return (FieldEdits()
            .with_value((0, "name"), "Ada Lovelace (née Byron)")
            .with_value((0, "notes"), "First line of notes that is long enough to wrap onto "
                                      "another line.\nA second paragraph.")
            .with_value((0, "agree"), "Yes")
            .with_value((0, "size"), "M")
            .with_value((0, "colour"), "Blue")
            .with_value((0, "person.name"), "Centred text")
            .with_value((0, "pick"), "second"))


def test_filled_form_matches_preview():
    for need in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp, "form.pdf"), Path(tmp, "out.pdf")
            _make_form(src, need_appearances=need)
            with PdfDocument(src) as doc:
                blank = [_shown(doc, i, scale=1.0) for i in (0, 1)]
                doc.fields = _fill(doc)
                preview = [_shown(doc, i, scale=1.0) for i in (0, 1)]
                commit(write_pdf(doc.sources, doc.pages, out, fields=doc.fields), out)
            with PdfDocument(out) as saved:
                for i in (0, 1):
                    got = _shown(saved, i, scale=1.0)
                    bad = _differing_pixels(got, preview[i])
                    assert bad == 0, f"NeedAppearances={need} page {i + 1}: {bad} pixels differ"
                    # page 2 changes by one small radio dot: ~40 pixels
                    assert _differing_pixels(got, blank[i]) > 20, "values not visible"
                values = {w.name: w.value for w in saved.page_widgets(0)}
                assert values["name"] == "Ada Lovelace (née Byron)"
                assert values["colour"] == "Blue" and values["agree"] == "Yes"
                on = [w.on_state for w in saved.page_widgets(0) if w.name == "size"
                      and is_on(w, 0, FieldEdits())]
                assert on == ["M"], on


def test_saved_values_are_real_form_data():
    """What Acrobat and form-data exports read: /V on the field, /AS on each
    button widget, and an appearance for each text widget."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "form.pdf"), Path(tmp, "out.pdf")
        _make_form(src)
        with PdfDocument(src) as doc:
            doc.fields = _fill(doc)
            commit(write_pdf(doc.sources, doc.pages, out, fields=doc.fields), out)
        with pikepdf.open(out) as pdf:
            by_name = {}
            for f in pdf.Root.AcroForm.Fields:
                by_name[str(f.T)] = f
            assert str(by_name["name"].V) == "Ada Lovelace (née Byron)"
            assert "/AP" in by_name["name"] and "/N" in by_name["name"].AP
            assert by_name["agree"].V == Name.Yes and by_name["agree"].AS == Name.Yes
            assert by_name["size"].V == Name("/M")
            assert [str(k.AS) for k in by_name["size"].Kids] == ["/Off", "/M", "/Off"]
            assert str(by_name["person"].Kids[0].V) == "Centred text"
            # /Opt radio: value "second" is widget 1, whose state name is "1"
            assert by_name["pick"].V == Name("/1")
            assert [str(k.AS) for k in by_name["pick"].Kids] == ["/Off", "/1"]


def test_unchecking_and_untouched_fields():
    with tempfile.TemporaryDirectory() as tmp:
        src, mid, out = Path(tmp, "form.pdf"), Path(tmp, "mid.pdf"), Path(tmp, "out.pdf")
        _make_form(src)
        with PdfDocument(src) as doc:
            doc.fields = _fill(doc)
            commit(write_pdf(doc.sources, doc.pages, mid, fields=doc.fields), mid)
        with PdfDocument(mid) as doc:
            doc.fields = FieldEdits().with_value((0, "agree"), "Off").with_value((0, "size"), "Off")
            preview = _shown(doc, 0, scale=1.0)
            commit(write_pdf(doc.sources, doc.pages, out, fields=doc.fields), out)
        with PdfDocument(out) as saved:
            assert _differing_pixels(_shown(saved, 0, scale=1.0), preview) == 0
            values = {w.name: w.value for w in saved.page_widgets(0)}
            assert values["agree"] == "Off" and values["size"] == "Off"
            assert values["name"] == "Ada Lovelace (née Byron)", "untouched field kept"


def test_filling_a_merged_copy_of_the_same_form():
    """Two copies of one form merged: field names clash and the copy's get
    renamed. Filling each keeps its own value."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "form.pdf"), Path(tmp, "out.pdf")
        _make_form(src)
        with PdfDocument(src) as doc:
            f = doc.add_source(src)
            doc.pages = doc.pages.inserted(doc.source_refs(f)[:1], 1)
            doc.fields = (FieldEdits().with_value((0, "name"), "Original")
                          .with_value((f, "name"), "The copy"))
            preview = [_shown(doc, i, scale=1.0) for i in range(doc.page_count)]
            commit(write_pdf(doc.sources, doc.pages, out, fields=doc.fields), out)
        with PdfDocument(out) as saved:
            for i in range(saved.page_count):
                assert _differing_pixels(_shown(saved, i, scale=1.0), preview[i]) == 0, f"page {i + 1}"
            first = {w.name: w.value for w in saved.page_widgets(0)}
            copy = {w.name: w.value for w in saved.page_widgets(1)}
            assert first["name"] == "Original"
            assert copy["name+1"] == "The copy", copy


# ---- creating and editing fields ---------------------------------------------------

def _flat_pdf(path):
    pdf = pikepdf.Pdf.new()
    helv = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].Resources = Dictionary(Font=Dictionary(F1=helv))
    pdf.pages[0].Contents = pdf.make_stream(
        b"BT /F1 12 Tf 72 740 Td (A flat document) Tj ET "
        b"0.5 G 72 690 m 400 690 l S 72 600 m 400 600 l S")
    pdf.save(path)


def _new_fields():
    return (new_field(TEXT, "Full name", (72, 692, 400, 712)),
            new_field(TEXT, "Comments", (72, 520, 400, 600), multiline=True),
            new_field(CHECKBOX, "Agreed", (72, 480, 86, 494)),
            new_field(COMBO, "Department", (72, 440, 220, 460), options=("IT", "HR", "Sales")))


def test_flat_pdf_becomes_a_form():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "flat.pdf"), Path(tmp, "out.pdf")
        _flat_pdf(src)
        with PdfDocument(src) as doc:
            assert not doc.has_forms and doc.page_widgets(0) == ()
            doc.forms = FormEdits().with_page(doc.page_key(0), _new_fields())
            assert [w.name for w in doc.current_widgets(0)][0] == "Full name"
            doc.fields = (FieldEdits().with_value((0, "Full name"), "Filled before saving")
                          .with_value((0, "Agreed"), "Yes"))
            preview = _shown(doc, 0, scale=1.0)
            commit(write_pdf(doc.sources, doc.pages, out, fields=doc.fields, forms=doc.forms), out)
        with PdfDocument(out) as saved:
            assert saved.has_forms
            assert _differing_pixels(_shown(saved, 0, scale=1.0), preview) == 0
            got = {w.name: (w.kind, w.value, w.options, w.multiline) for w in saved.page_widgets(0)}
            assert got == {
                "Full name": (TEXT, "Filled before saving", (), False),
                "Comments": (TEXT, "", (), True),
                "Agreed": (CHECKBOX, "Yes", (), False),
                "Department": (COMBO, "", ("IT", "HR", "Sales"), False),
            }, got


def test_editing_an_existing_form():
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "form.pdf"), Path(tmp, "out.pdf")
        _make_form(src)
        with PdfDocument(src) as doc:
            doc.fields = FieldEdits().with_value((0, "colour"), "Green")
            widgets = []
            for w in doc.page_widgets(0):
                if w.name == "agree":
                    continue                                   # delete
                if w.name == "name":
                    w = w.moved_to((150, 720, 430, 742))       # move
                elif w.name == "notes":
                    w = w.moved_to((120, 500, 500, 650))       # resize
                elif w.name == "colour":
                    w = replace(w.renamed("favourite colour"), font_size=14,
                                options=("Green", "Purple"))
                widgets.append(w)
            doc.forms = FormEdits().with_page(doc.page_key(0), widgets)
            doc.fields = doc.fields.renamed(0, "colour", "favourite colour")
            preview = _shown(doc, 0, scale=1.0)
            commit(write_pdf(doc.sources, doc.pages, out, fields=doc.fields, forms=doc.forms), out)
        with PdfDocument(out) as saved:
            assert _differing_pixels(_shown(saved, 0, scale=1.0), preview) == 0
            got = {w.name: w for w in saved.page_widgets(0)}
            assert "agree" not in got and "colour" not in got
            assert [round(v) for v in got["name"].rect] == [150, 720, 430, 742]
            fav = got["favourite colour"]
            assert fav.options == ("Green", "Purple") and fav.value == "Green" and fav.font_size == 14
        with pikepdf.open(out) as pdf:
            names = [str(f.T) for f in pdf.Root.AcroForm.Fields]
            assert "agree" not in names, "deleted field left in /AcroForm"
            assert not any(a is None for a in pdf.pages[0].obj.Annots), "placeholder left behind"


def test_deleting_a_field_does_not_shift_annotation_edits():
    """A deleted widget is a placeholder until the end, so annotation edits
    addressed by /Annots index still hit the right annotation."""
    from test_annots import _make_annotated_pdf
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "a.pdf"), Path(tmp, "out.pdf")
        _make_annotated_pdf(src)      # page 2: [field widget, square, text box]
        with PdfDocument(src) as doc:
            doc.forms = FormEdits().with_page(doc.page_key(1), ())       # delete the field
            moved = tuple(a.moved(0, -100) if a.kind == "Square" else a for a in doc.file_annots(1))
            doc.annots = doc.annots.with_page(doc.page_key(1), moved)
            preview = _shown(doc, 1, scale=1.0)
            commit(write_pdf(doc.sources, doc.pages, out, annots=doc.annots, forms=doc.forms), out)
        with PdfDocument(out) as saved:
            assert _differing_pixels(_shown(saved, 1, scale=1.0), preview) == 0
            assert saved.page_widgets(1) == ()
            square = next(a for a in saved.file_annots(1) if a.kind == "Square")
            assert [round(v) for v in square.rect] == [300, 100, 400, 200], "wrong annotation moved"
            assert [a.contents for a in saved.file_annots(1) if a.kind == "FreeText"] == ["Typed elsewhere"]


def test_new_field_on_a_merged_page():
    with tempfile.TemporaryDirectory() as tmp:
        base, flat, out = Path(tmp, "form.pdf"), Path(tmp, "flat.pdf"), Path(tmp, "out.pdf")
        _make_form(base)
        _flat_pdf(flat)
        with PdfDocument(base) as doc:
            f = doc.add_source(flat)
            doc.pages = doc.pages.inserted(doc.source_refs(f), 1)
            doc.forms = FormEdits().with_page(doc.page_key(1), _new_fields()[:1])
            doc.fields = FieldEdits().with_value((f, "Full name"), "On the inserted page")
            preview = _shown(doc, 1, scale=1.0)
            commit(write_pdf(doc.sources, doc.pages, out, fields=doc.fields, forms=doc.forms), out)
        with PdfDocument(out) as saved:
            assert _differing_pixels(_shown(saved, 1, scale=1.0), preview) == 0
            assert {w.name: w.value for w in saved.page_widgets(1)} == {"Full name": "On the inserted page"}
            assert "name" in {w.name for w in saved.page_widgets(0)}, "original form intact"


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
