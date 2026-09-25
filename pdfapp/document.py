"""Document model. Deliberately free of any Qt import so it can be tested headless."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import pypdfium2 as pdfium

from .pagelist import PageList, PageRef
from .transform import PageTransform, normalize_rotation


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
        self._closed = False

    # ---- lifecycle -----------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for source in self._sources:
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

    def render_bitmap(self, index: int, scale: float, view_rotation: int = 0):
        """Render a page and return the raw pdfium bitmap.

        Caller owns the bitmap and must close it. Use PageRenderer instead
        unless you have a reason not to.
        """
        page = self._page(index)
        # pdfium applies the intrinsic /Rotate itself; we add the edit
        # rotation on top, exactly as saving it into /Rotate would.
        return page.render(
            scale=scale,
            rotation=(self._pages[index].rotation + view_rotation) % 360,
            may_draw_forms=self._source(index).has_forms,
            prefer_bgrx=True,  # 32-bit aligned rows blit faster into Qt
        )
