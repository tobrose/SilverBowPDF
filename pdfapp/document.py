"""Document model. Deliberately free of any Qt import so it can be tested headless."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .annots import MARKUP_KINDS, Annot, AnnotEdits, PageKey, is_editable
from .fields import CHECKBOX, COMBO, RADIO, TEXT, FieldEdits, FormEdits, Widget
from .pagelist import PageList, PageRef
from .textlayout import parse_da
from .transform import PageTransform, normalize_rotation

# Acrobat's light field blue, RGB(204, 215, 255). pdfium's header says the
# colour is 0x00RRGGBB, but it reads it as a Windows COLORREF, 0x00BBGGRR
# (measured: 0xCCD7FF came out peach). tests/test_annots checks the tint.
FIELD_HIGHLIGHT = 0xFFD7CC
FIELD_HIGHLIGHT_ALPHA = 90

_ANNOT_KINDS = {
    getattr(pdfium_c, f"FPDF_ANNOT_{name.upper()}"): name
    for name in ("Text", "Link", "FreeText", "Line", "Square", "Circle", "Polygon",
                 "PolyLine", "Highlight", "Underline", "Squiggly", "StrikeOut", "Stamp",
                 "Caret", "Ink", "Popup", "FileAttachment", "Sound", "Movie", "Widget",
                 "Screen", "PrinterMark", "TrapNet", "Watermark", "RichMedia",
                 "XFAWidget", "Redact")
}


class PasswordRequired(Exception):
    """The file is encrypted and the supplied password (if any) was wrong."""


class DocumentError(Exception):
    """The file could not be opened as a PDF."""


@dataclass(frozen=True)
class SourceInfo:
    """What the writer needs to reopen a source file with pikepdf."""

    path: Path
    password: Optional[str]


@dataclass
class _Source:
    info: SourceInfo
    pdf: pdfium.PdfDocument
    page_count: int
    has_forms: bool
    form_type: int = 0
    page_cache: dict = field(default_factory=dict)  # source index -> PdfPage
    text_cache: dict = field(default_factory=dict)  # source index -> _PageText


@dataclass
class _PageText:
    textpage: pdfium.PdfTextPage
    boxes: List[Tuple[float, float, float, float]]  # loose char boxes, PDF space


def _open_source(path: Path, password: Optional[str]) -> _Source:
    try:
        pdf = pdfium.PdfDocument(str(path), password=password)
        # Touch the page count so a bad password fails here, not later.
        count = len(pdf)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise PasswordRequired(str(exc)) from exc
        raise DocumentError(f"could not open {path.name}: {exc}") from exc

    # Form fields only render if the form environment is initialised.
    form_type = 0
    try:
        form_type = pdf.get_formtype()
        has_forms = form_type != 0
        if has_forms:
            pdf.init_forms()
            # Fields often have no border or background of their own; every
            # mainstream viewer tints them so they can be found. Without this
            # an empty form looks like a blank page.
            pdfium_c.FPDF_SetFormFieldHighlightColor(pdf.formenv, 0, FIELD_HIGHLIGHT)
            pdfium_c.FPDF_SetFormFieldHighlightAlpha(pdf.formenv, FIELD_HIGHLIGHT_ALPHA)
    except Exception:
        has_forms = False
    return _Source(SourceInfo(path, password), pdf, count, has_forms, form_type)


class PdfDocument:
    """Read-only view of one or more PDFs for rendering and measurement.

    This class exists purely to answer "how big is page N and what does it
    look like at scale S". Page operations don't mutate anything: `pages` maps
    each displayed index to a (file, page, extra rotation), and every public
    method here takes a *displayed* index. Writing the edits out is
    writer.py's job, via pikepdf.

    File 0 is the document that was opened. Inserting pages from another PDF
    opens it as an additional source; sources are only ever added, so any
    PageList in the undo history stays resolvable.
    """

    def __init__(self, path: str | Path, password: Optional[str] = None):
        self._sources: List[_Source] = [_open_source(Path(path), password)]
        self._pages = PageList.identity(self._sources[0].page_count)
        self._annots = AnnotEdits()
        self._fields = FieldEdits()
        self._forms = FormEdits()
        self._file_annots: Dict[PageKey, Tuple[Annot, ...]] = {}
        self._widgets: Dict[PageKey, Tuple[Widget, ...]] = {}
        self._closed = False

    # ---- lifecycle -----------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for source in self._sources:
            for text in source.text_cache.values():
                text.textpage.close()
            source.text_cache.clear()
            source.page_cache.clear()
            try:
                source.pdf.close()
            except Exception:
                pass

    def __enter__(self) -> "PdfDocument":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- sources -------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._sources[0].info.path

    @property
    def password(self) -> Optional[str]:
        return self._sources[0].info.password

    @property
    def sources(self) -> List[SourceInfo]:
        return [s.info for s in self._sources]

    def add_source(self, path: str | Path, password: Optional[str] = None) -> int:
        """Open another PDF to take pages from. Returns its file index.

        Always a new index, even for a file that's already a source (including
        this document itself): each (file, page) appears at most once in a
        PageList, so inserting the same file twice needs two identities.
        """
        if self._closed:
            raise DocumentError("document is closed")
        self._sources.append(_open_source(Path(path), password))
        return len(self._sources) - 1

    def source_refs(self, file: int) -> List[PageRef]:
        """One PageRef per page of source `file`, in file order."""
        return [PageRef(i, 0, file) for i in range(self._sources[file].page_count)]

    # ---- structure -----------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def source_page_count(self) -> int:
        """Pages in the opened file (file 0)."""
        return self._sources[0].page_count

    @property
    def pages(self) -> PageList:
        return self._pages

    @pages.setter
    def pages(self, pages: PageList) -> None:
        if len(pages) == 0:
            raise ValueError("a document needs at least one page")
        for ref in pages:
            if not 0 <= ref.file < len(self._sources):
                raise IndexError(f"source file {ref.file} is not open")
            if not 0 <= ref.source < self._sources[ref.file].page_count:
                raise IndexError(f"page {ref.source} not in {self._sources[ref.file].info.path.name}")
        self._pages = pages

    # ---- annotations ---------------------------------------------------

    @property
    def annots(self) -> AnnotEdits:
        return self._annots

    @annots.setter
    def annots(self, annots: AnnotEdits) -> None:
        self._annots = annots

    @property
    def fields(self) -> FieldEdits:
        return self._fields

    @fields.setter
    def fields(self, fields: FieldEdits) -> None:
        self._fields = fields

    @property
    def forms(self) -> FormEdits:
        return self._forms

    @forms.setter
    def forms(self, forms: FormEdits) -> None:
        self._forms = forms

    def current_widgets(self, index: int) -> Tuple[Widget, ...]:
        """The page's widgets as currently edited (fields added, moved,
        deleted...); the file's when its form structure is unchanged."""
        edited = self._forms.get(self.page_key(index))
        return edited if edited is not None else self.page_widgets(index)

    def page_widgets(self, index: int) -> Tuple[Widget, ...]:
        """The page's form widgets as the file has them (values included).
        Read through pdfium's form API, which resolves inherited field
        attributes and names; origin lines up with /Annots like annotations."""
        key = self.page_key(index)
        cached = self._widgets.get(key)
        if cached is not None:
            return cached
        source = self._source(index)
        formenv = source.pdf.formenv if source.has_forms else None
        found: List[Widget] = []
        if formenv:
            page = self._page(index)
            for i in range(pdfium_c.FPDFPage_GetAnnotCount(page)):
                handle = pdfium_c.FPDFPage_GetAnnot(page, i)
                if not handle:
                    continue
                try:
                    if pdfium_c.FPDFAnnot_GetSubtype(handle) != pdfium_c.FPDF_ANNOT_WIDGET:
                        continue
                    rect = pdfium_c.FS_RECTF()
                    if not pdfium_c.FPDFAnnot_GetRect(handle, rect):
                        continue
                    found.append(_read_widget(formenv, handle, i, rect))
                finally:
                    pdfium_c.FPDFPage_CloseAnnot(handle)
        result = tuple(found)
        self._widgets[key] = result
        return result

    def page_key(self, index: int) -> PageKey:
        ref = self._pages[index]
        return (ref.file, ref.source)

    def page_annots(self, index: int) -> Tuple[Annot, ...]:
        """The page's editable annotations as currently edited."""
        edited = self._annots.get(self.page_key(index))
        return edited if edited is not None else self.file_annots(index)

    def file_annots(self, index: int) -> Tuple[Annot, ...]:
        """The page's editable annotations as they are in the file.

        Read through pdfium's annotation API. FPDFPage_GetAnnot indexes the
        page's /Annots array directly, which is what makes `origin` line up
        with the index the writer sees through pikepdf.
        """
        key = self.page_key(index)
        cached = self._file_annots.get(key)
        if cached is not None:
            return cached
        page = self._page(index)
        found: List[Annot] = []
        for i in range(pdfium_c.FPDFPage_GetAnnotCount(page)):
            handle = pdfium_c.FPDFPage_GetAnnot(page, i)
            if not handle:
                continue
            try:
                kind = _ANNOT_KINDS.get(pdfium_c.FPDFAnnot_GetSubtype(handle), "Unknown")
                if not is_editable(kind, pdfium_c.FPDFAnnot_GetFlags(handle)):
                    continue
                rect = pdfium_c.FS_RECTF()
                if not pdfium_c.FPDFAnnot_GetRect(handle, rect):
                    continue
                extra = {}
                if kind in ("Square", "Circle", "Line", "Ink", "Polygon", "PolyLine"):
                    extra["width"] = _border_width(handle) or 1.0
                if kind == "Line":
                    extra["line"] = _line_points(handle)
                elif kind == "Ink":
                    extra["ink"] = _ink_strokes(handle)
                if kind == "FreeText":
                    size, color = parse_da(_annot_string(handle, b"DA"))
                    extra = dict(font_size=size, color=color, boxed=_border_width(handle) > 0,
                                 fixed_width=True,  # respect the width it was given
                                 text_rotation=int(page.get_rotation()))
                found.append(Annot(
                    kind, (rect.left, rect.bottom, rect.right, rect.top),
                    contents=_annot_string(handle, b"Contents"),
                    author=_annot_string(handle, b"T"),
                    origin=i,
                    reply_to=_reply_target(page, handle),
                    quads=_quad_points(handle) if kind in MARKUP_KINDS else (),
                    **extra,
                ))
            finally:
                pdfium_c.FPDFPage_CloseAnnot(handle)
        result = tuple(found)
        self._file_annots[key] = result
        return result

    # ---- text ----------------------------------------------------------
    #
    # Character indices are pdfium's: content-stream order, which is reading
    # order for nearly everything, plus generated line breaks. Ranges are
    # inclusive (start, end) and may come in either order.

    def _text(self, index: int) -> _PageText:
        if self._closed:
            raise DocumentError("document is closed")
        ref = self._pages[index]
        source = self._sources[ref.file]
        text = source.text_cache.get(ref.source)
        if text is None:
            textpage = self._page(index).get_textpage()
            boxes = [textpage.get_charbox(i, loose=True) for i in range(textpage.count_chars())]
            text = _PageText(textpage, boxes)
            source.text_cache[ref.source] = text
        return text

    def char_count(self, index: int) -> int:
        return len(self._text(index).boxes)

    def char_boxes(self, index: int) -> List[Tuple[float, float, float, float]]:
        """Loose character boxes (left, bottom, right, top), PDF space."""
        return self._text(index).boxes

    def char_at(self, index: int, x: float, y: float, tolerance: float = 1.0) -> int:
        """Character under a PDF-space point, or -1. pdfium's own hit test --
        this runs on every mouse move while hovering."""
        found = self._text(index).textpage.get_index(x, y, tolerance, tolerance)
        return -1 if found is None else found

    def nearest_char(self, index: int, x: float, y: float) -> int:
        """Closest character to a point, or -1 on a page with no text. For
        drags that wander into margins and gaps between lines."""
        best, best_d = -1, float("inf")
        for i, (left, bottom, right, top) in enumerate(self._text(index).boxes):
            if right <= left or top <= bottom:
                continue  # generated characters (line breaks) have no area
            dx = max(left - x, 0.0, x - right)
            dy = max(bottom - y, 0.0, y - top)
            d = dx * dx + dy * dy * 4  # prefer the same line over a nearer line
            if d < best_d:
                best, best_d = i, d
        return best

    def word_at(self, index: int, char: int) -> Tuple[int, int]:
        """Inclusive character range of the word containing `char`."""
        textpage = self._text(index).textpage
        n = self.char_count(index)
        text = textpage.get_text_range(0, n)
        if len(text) != n or not 0 <= char < n or not text[char].isalnum():
            return char, char
        start, end = char, char
        while start > 0 and text[start - 1].isalnum():
            start -= 1
        while end < n - 1 and text[end + 1].isalnum():
            end += 1
        return start, end

    def text_in(self, index: int, start: int, end: int) -> str:
        start, end = sorted((start, end))
        return self._text(index).textpage.get_text_range(start, end - start + 1)

    def text_rects(self, index: int, start: int, end: int) -> List[Tuple[float, float, float, float]]:
        """The selected range as rectangles, one per line segment, PDF space.
        pdfium merges characters on a line; this is what highlights cover."""
        start, end = sorted((start, end))
        textpage = self._text(index).textpage
        count = textpage.count_rects(start, end - start + 1)
        return [textpage.get_rect(i) for i in range(count)]

    def find_text(self, index: int, query: str, match_case: bool = False,
                  whole_word: bool = False) -> List[Tuple[int, int, list]]:
        """Every occurrence of `query` on a page, in order: (start, end,
        rects), the range inclusive and the rects as text_rects gives them.

        pdfium's own search, so the ranges are the same char indices as text
        selection. A page whose text isn't cached already gets a throwaway
        text page: searching a long document must not pin every page's
        text (and char boxes) in memory for the rest of the session.
        """
        if not query:
            return []
        if self._closed:
            raise DocumentError("document is closed")
        ref = self._pages[index]
        source = self._sources[ref.file]
        cached = source.text_cache.get(ref.source)
        page = textpage = None
        own_page = False
        if cached is not None:
            textpage = cached.textpage
        else:
            page = source.page_cache.get(ref.source)
            if page is None:
                page, own_page = source.pdf[ref.source], True
            textpage = page.get_textpage()
        try:
            hits = []
            searcher = textpage.search(query, match_case=match_case, match_whole_word=whole_word)
            try:
                while (found := searcher.get_next()) is not None:
                    start, count = found
                    rects = [textpage.get_rect(i)
                             for i in range(textpage.count_rects(start, count))]
                    hits.append((start, start + count - 1, rects))
            finally:
                searcher.close()
            return hits
        finally:
            if cached is None:
                textpage.close()
                if own_page:
                    page.close()

    def page_geometry(self, index: int):
        """(MediaBox, CropBox, intrinsic /Rotate) with inheritance resolved."""
        page = self._page(index)
        media = tuple(float(v) for v in page.get_mediabox())
        return media, self.page_box(index), int(page.get_rotation())

    @property
    def has_forms(self) -> bool:
        return any(s.has_forms for s in self._sources)

    @property
    def is_xfa(self) -> bool:
        """An XFA (Adobe LiveCycle) form. pdfium -- like Chrome and Edge --
        only shows the fallback page content, which often has no fields."""
        return self._sources[0].form_type in (pdfium_c.FORMTYPE_XFA_FULL,
                                                pdfium_c.FORMTYPE_XFA_FOREGROUND)

    def page_has_forms(self, index: int) -> bool:
        return self._source(index).has_forms

    @property
    def title(self) -> str:
        try:
            meta = self._sources[0].pdf.get_metadata_dict()
            return (meta.get("Title") or "").strip() or self.path.name
        except Exception:
            return self.path.name

    def _source(self, index: int) -> _Source:
        return self._sources[self._pages[index].file]

    def _page(self, index: int) -> pdfium.PdfPage:
        # pdfium dereferences freed memory on a closed document -- an access
        # violation, not an exception. Fail loudly and catchably instead.
        if self._closed:
            raise DocumentError("document is closed")
        if not 0 <= index < self.page_count:
            raise IndexError(f"page {index} out of range (0..{self.page_count - 1})")
        ref = self._pages[index]
        source = self._sources[ref.file]
        page = source.page_cache.get(ref.source)
        if page is None:
            page = source.pdf[ref.source]
            source.page_cache[ref.source] = page
        return page

    def page_box(self, index: int) -> Tuple[float, float, float, float]:
        """The CropBox in PDF user space, unrotated. Falls back to MediaBox."""
        page = self._page(index)
        try:
            box = tuple(float(v) for v in page.get_cropbox())
        except Exception:
            box = tuple(float(v) for v in page.get_mediabox())
        if box[2] - box[0] == 0 or box[3] - box[1] == 0:
            box = tuple(float(v) for v in page.get_mediabox())
        return box  # type: ignore[return-value]

    def page_rotation(self, index: int) -> int:
        """The page's own /Rotate plus any edit rotation, degrees clockwise.

        This is the rotation the page will have once saved, so the transform
        built from it matches what pdfium renders now and what the file will
        contain later.
        """
        try:
            intrinsic = int(self._page(index).get_rotation())
        except Exception:
            intrinsic = 0
        return normalize_rotation(intrinsic + self._pages[index].rotation)

    def transform(
        self,
        index: int,
        scale: float = 1.0,
        view_rotation: int = 0,
        offset: Tuple[float, float] = (0.0, 0.0),
    ) -> PageTransform:
        """Build the PageTransform for a page at a given zoom and view rotation."""
        return PageTransform(
            box=self.page_box(index),
            rotation=self.page_rotation(index) + view_rotation,
            scale=scale,
            offset=offset,
        )

    def widest_display_width_pt(self, view_rotation: int = 0) -> float:
        """Widest page in the document, in points, as displayed.

        Used to pick a fit-width zoom that doesn't make the layout jump around
        every time the user scrolls past a landscape page.
        """
        widest = 1.0
        for i in range(self.page_count):
            widest = max(widest, self.transform(i, view_rotation=view_rotation).display_size_pt[0])
        return widest

    def tallest_display_height_pt(self, view_rotation: int = 0) -> float:
        tallest = 1.0
        for i in range(self.page_count):
            tallest = max(
                tallest, self.transform(i, view_rotation=view_rotation).display_size_pt[1]
            )
        return tallest

    # ---- rendering ------------------------------------------------------

    def render_bitmap(self, index: int, scale: float, view_rotation: int = 0,
                      overlay: Optional[bytes] = None, for_print: bool = False):
        """Render a page and return the raw pdfium bitmap.

        for_print: as printed rather than as shown -- pdfium's printing mode
        (annotations flagged not to print are left out, NoView ones drawn)
        and without the on-screen tint that marks form fields.

        Caller owns the bitmap and must close it. Use PageRenderer instead
        unless you have a reason not to.

        overlay: a one-page PDF of this page's annotations with edits applied
        (annotpdf.overlay_pdf), form widgets left out. The page is then drawn
        in three passes: content, form fields, annotations. Annotations go
        LAST, unlike pdfium's own render (which draws fields over them), so
        a pen stroke across a checkbox lies on top of it, as on paper and in
        Acrobat. The renderer uses this path for any page with annotation
        edits, and for any form page carrying other annotations, so a page
        is drawn the same way before and after saving.
        """
        page = self._page(index)
        source = self._source(index)
        if not for_print:
            return self._render(page, source, index, scale, view_rotation, overlay, 0)
        # The field tint is a setting on the document's form engine, shared
        # by every page: off for exactly this render, back on after.
        tint = source.has_forms and source.pdf.formenv
        if tint:
            pdfium_c.FPDF_SetFormFieldHighlightAlpha(source.pdf.formenv, 0)
        try:
            return self._render(page, source, index, scale, view_rotation, overlay,
                                pdfium_c.FPDF_PRINTING)
        finally:
            if tint:
                pdfium_c.FPDF_SetFormFieldHighlightAlpha(source.pdf.formenv, FIELD_HIGHLIGHT_ALPHA)

    def _render(self, page, source, index, scale, view_rotation, overlay, extra_flags: int):
        # pdfium applies the intrinsic /Rotate itself; we add the edit
        # rotation on top, exactly as saving it into /Rotate would.
        rotation = (self._pages[index].rotation + view_rotation) % 360
        printing = bool(extra_flags & pdfium_c.FPDF_PRINTING)
        if overlay is None:
            return page.render(
                scale=scale,
                rotation=rotation,
                may_draw_forms=source.has_forms,
                prefer_bgrx=True,  # 32-bit aligned rows blit faster into Qt
                extra_flags=extra_flags,
            )

        bitmap = page.render(scale=scale, rotation=rotation, draw_annots=False,
                             may_draw_forms=False, prefer_bgrx=True, extra_flags=extra_flags)
        flags = pdfium_c.FPDF_ANNOT | extra_flags
        # Exactly the placement page.render used (no crop): origin, full
        # size, rotation as a quarter-turn count.
        placement = (0, 0, bitmap.width, bitmap.height, rotation // 90)
        overlay_doc = pdfium.PdfDocument(overlay)
        try:
            if len(overlay_doc) == 2:
                # Filled-in fields: the overlay's first page holds this page's
                # widgets with the new values; its form engine draws them.
                overlay_doc.init_forms()
                pdfium_c.FPDF_SetFormFieldHighlightColor(overlay_doc.formenv, 0, FIELD_HIGHLIGHT)
                pdfium_c.FPDF_SetFormFieldHighlightAlpha(
                    overlay_doc.formenv, 0 if printing else FIELD_HIGHLIGHT_ALPHA)
                form_page = overlay_doc[0]
                try:
                    pdfium_c.FPDF_FFLDraw(overlay_doc.formenv, bitmap, form_page, *placement, flags)
                finally:
                    form_page.close()
            else:
                formenv = getattr(page, "formenv", None)
                if source.has_forms and formenv:
                    pdfium_c.FPDF_FFLDraw(formenv, bitmap, page, *placement, flags)
            overlay_page = overlay_doc[len(overlay_doc) - 1]
            try:
                pdfium_c.FPDF_RenderPageBitmap(bitmap, overlay_page, *placement, flags)
            finally:
                overlay_page.close()
        finally:
            overlay_doc.close()
        return bitmap


_FIELD_KINDS = {
    pdfium_c.FPDF_FORMFIELD_TEXTFIELD: TEXT, pdfium_c.FPDF_FORMFIELD_CHECKBOX: CHECKBOX,
    pdfium_c.FPDF_FORMFIELD_RADIOBUTTON: RADIO, pdfium_c.FPDF_FORMFIELD_COMBOBOX: COMBO,
}


def _form_string(func, formenv, handle, *args) -> str:
    size = func(formenv, handle, *args, None, 0)
    if size <= 2:
        return ""
    buffer = ctypes.create_string_buffer(size)
    func(formenv, handle, *args, ctypes.cast(buffer, ctypes.POINTER(pdfium_c.FPDF_WCHAR)), size)
    return buffer.raw[:size - 2].decode("utf-16-le", errors="replace")


def _read_widget(formenv, handle, index: int, rect) -> Widget:
    kind = _FIELD_KINDS.get(pdfium_c.FPDFAnnot_GetFormFieldType(formenv, handle), "other")
    flags = pdfium_c.FPDFAnnot_GetFormFieldFlags(formenv, handle)
    name = _form_string(pdfium_c.FPDFAnnot_GetFormFieldName, formenv, handle)
    value = _form_string(pdfium_c.FPDFAnnot_GetFormFieldValue, formenv, handle)
    on_state, options = "", ()
    if kind in (CHECKBOX, RADIO):
        # pdfium can't list /AP /N state names, but the export value IS the
        # on-state name unless the field has /Opt -- and for /Opt fields the
        # writer maps export values back to real state names (formpdf).
        on_state = _form_string(pdfium_c.FPDFAnnot_GetFormFieldExportValue, formenv, handle)
        value = on_state if pdfium_c.FPDFAnnot_IsChecked(formenv, handle) else "Off"
    elif kind == COMBO:
        options = tuple(
            _form_string(pdfium_c.FPDFAnnot_GetOptionLabel, formenv, handle, n)
            for n in range(max(0, pdfium_c.FPDFAnnot_GetOptionCount(formenv, handle))))
    size = ctypes.c_float(0.0)
    font_size = size.value if pdfium_c.FPDFAnnot_GetFontSize(formenv, handle, size) else 0.0
    return Widget(index, (rect.left, rect.bottom, rect.right, rect.top), name, kind, flags,
                  value, on_state, options, font_size)


def _line_points(handle) -> Tuple[float, ...]:
    start, end = pdfium_c.FS_POINTF(), pdfium_c.FS_POINTF()
    if pdfium_c.FPDFAnnot_GetLine(handle, start, end):
        return (start.x, start.y, end.x, end.y)
    return ()


def _ink_strokes(handle) -> Tuple[Tuple[float, ...], ...]:
    strokes = []
    for i in range(pdfium_c.FPDFAnnot_GetInkListCount(handle)):
        n = pdfium_c.FPDFAnnot_GetInkListPath(handle, i, None, 0)
        if n <= 0:
            continue
        points = (pdfium_c.FS_POINTF * n)()
        pdfium_c.FPDFAnnot_GetInkListPath(handle, i, points, n)
        strokes.append(tuple(v for pt in points for v in (pt.x, pt.y)))
    return tuple(strokes)


def _border_width(handle) -> float:
    h, v, w = ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
    if pdfium_c.FPDFAnnot_GetBorder(handle, h, v, w):
        return w.value
    return 0.0


def _quad_points(handle) -> Tuple[float, ...]:
    """A markup annotation's /QuadPoints, for hit-testing line by line."""
    quads: List[float] = []
    for i in range(pdfium_c.FPDFAnnot_CountAttachmentPoints(handle)):
        q = pdfium_c.FS_QUADPOINTSF()
        if pdfium_c.FPDFAnnot_GetAttachmentPoints(handle, i, q):
            quads += [q.x1, q.y1, q.x2, q.y2, q.x3, q.y3, q.x4, q.y4]
    return tuple(quads)


def _reply_target(page, handle) -> Optional[int]:
    """/Annots index of the annotation this one replies to (/IRT), if any."""
    linked = pdfium_c.FPDFAnnot_GetLinkedAnnot(handle, b"IRT")
    if not linked:
        return None
    try:
        index = pdfium_c.FPDFPage_GetAnnotIndex(page, linked)
        return index if index >= 0 else None
    finally:
        pdfium_c.FPDFPage_CloseAnnot(linked)


def _annot_string(handle, key: bytes) -> str:
    """A text value of an annotation (UTF-16LE from pdfium), or ''."""
    size = pdfium_c.FPDFAnnot_GetStringValue(handle, key, None, 0)
    if size <= 2:
        return ""
    buffer = ctypes.create_string_buffer(size)
    pdfium_c.FPDFAnnot_GetStringValue(handle, key, ctypes.cast(buffer, ctypes.POINTER(pdfium_c.FPDF_WCHAR)), size)
    return buffer.raw[:size - 2].decode("utf-16-le", errors="replace")
