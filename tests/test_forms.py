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
from pdfapp.fields import CHECKBOX, COMBO, RADIO, TEXT, FieldEdits, is_on  # noqa: E402
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
