"""Document model. Deliberately free of any Qt import so it can be tested headless."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from .annots import MARKUP_KINDS, Annot, AnnotEdits, PageKey, is_editable
from .pagelist import PageList, PageRef
from .transform import PageTransform, normalize_rotation

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
    try:
        has_forms = pdf.get_formtype() != 0
        if has_forms:
            pdf.init_forms()
    except Exception:
        has_forms = False
    return _Source(SourceInfo(path, password), pdf, count, has_forms)


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
        self._file_annots: Dict[PageKey, Tuple[Annot, ...]] = {}
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
                found.append(Annot(
                    kind, (rect.left, rect.bottom, rect.right, rect.top),
                    contents=_annot_string(handle, b"Contents"),
                    author=_annot_string(handle, b"T"),
                    origin=i,
                    reply_to=_reply_target(page, handle),
                    quads=_quad_points(handle) if kind in MARKUP_KINDS else (),
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

    def page_geometry(self, index: int):
        """(MediaBox, CropBox, intrinsic /Rotate) with inheritance resolved."""
        page = self._page(index)
        media = tuple(float(v) for v in page.get_mediabox())
        return media, self.page_box(index), int(page.get_rotation())

    @property
    def has_forms(self) -> bool:
        return any(s.has_forms for s in self._sources)

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
                      overlay: Optional[bytes] = None):
        """Render a page and return the raw pdfium bitmap.

        Caller owns the bitmap and must close it. Use PageRenderer instead
        unless you have a reason not to.

        overlay: a one-page PDF of this page's annotations with edits applied
        (annotpdf.overlay_pdf). The page is then drawn in the same three
        passes pdfium's normal render makes -- content, annotations, form
        fields -- except the annotations come from the overlay. Same boxes,
        same rotation, same bitmap: the result is what the saved page renders.
        """
        page = self._page(index)
        source = self._source(index)
        # pdfium applies the intrinsic /Rotate itself; we add the edit
        # rotation on top, exactly as saving it into /Rotate would.
        rotation = (self._pages[index].rotation + view_rotation) % 360
        if overlay is None:
            return page.render(
                scale=scale,
                rotation=rotation,
                may_draw_forms=source.has_forms,
                prefer_bgrx=True,  # 32-bit aligned rows blit faster into Qt
            )

        bitmap = page.render(scale=scale, rotation=rotation, draw_annots=False,
                             may_draw_forms=False, prefer_bgrx=True)
        # Exactly the placement page.render used (no crop): origin, full
        # size, rotation as a quarter-turn count.
        placement = (0, 0, bitmap.width, bitmap.height, rotation // 90)
        overlay_doc = pdfium.PdfDocument(overlay)
        try:
            overlay_page = overlay_doc[0]
            try:
                pdfium_c.FPDF_RenderPageBitmap(bitmap, overlay_page, *placement,
                                               pdfium_c.FPDF_ANNOT)
            finally:
                overlay_page.close()
        finally:
            overlay_doc.close()
        formenv = getattr(page, "formenv", None)
        if source.has_forms and formenv:
            pdfium_c.FPDF_FFLDraw(formenv, bitmap, page, *placement, pdfium_c.FPDF_ANNOT)
        return bitmap


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
