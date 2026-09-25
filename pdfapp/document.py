"""Document model. Deliberately free of any Qt import so it can be tested headless."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import pypdfium2 as pdfium

from .pagelist import PageList
from .transform import PageTransform, normalize_rotation


class PasswordRequired(Exception):
    """The file is encrypted and the supplied password (if any) was wrong."""


class DocumentError(Exception):
    """The file could not be opened as a PDF."""


class PdfDocument:
    """Read-only view of a PDF for rendering and measurement.

    This class exists purely to answer "how big is page N and what does it
    look like at scale S". Page operations don't mutate anything: `pages` maps
    each displayed index to a source page plus extra rotation, and every
    public method here takes a *displayed* index. Writing the edits out is
    writer.py's job, via pikepdf.
    """

    def __init__(self, path: str | Path, password: Optional[str] = None):
        self.path = Path(path)
        self.password = password
        try:
            self._pdf = pdfium.PdfDocument(str(self.path), password=password)
            # Touch the page count so a bad password fails here, not later.
            _ = len(self._pdf)
        except pdfium.PdfiumError as exc:
            msg = str(exc).lower()
            if "password" in msg:
                raise PasswordRequired(str(exc)) from exc
            raise DocumentError(f"could not open {self.path.name}: {exc}") from exc

        # Form fields only render if the form environment is initialised.
        try:
            if self._pdf.get_formtype() != 0:
                self._pdf.init_forms()
                self._has_forms = True
            else:
                self._has_forms = False
        except Exception:
            self._has_forms = False

        self._page_cache: dict[int, pdfium.PdfPage] = {}  # keyed by SOURCE index
        self._source_count = len(self._pdf)
        self._pages = PageList.identity(self._source_count)
        self._closed = False

    # ---- lifecycle -----------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._page_cache.clear()
        try:
            self._pdf.close()
        except Exception:
            pass

    def __enter__(self) -> "PdfDocument":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- structure -----------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def source_page_count(self) -> int:
        return self._source_count

    @property
    def pages(self) -> PageList:
        return self._pages

    @pages.setter
    def pages(self, pages: PageList) -> None:
        if len(pages) == 0:
            raise ValueError("a document needs at least one page")
        for ref in pages:
            if not 0 <= ref.source < self._source_count:
                raise IndexError(f"source page {ref.source} not in this file")
        self._pages = pages

    @property
    def has_forms(self) -> bool:
        return self._has_forms

    @property
    def title(self) -> str:
        try:
            meta = self._pdf.get_metadata_dict()
            return (meta.get("Title") or "").strip() or self.path.name
        except Exception:
            return self.path.name

    def _page(self, index: int) -> pdfium.PdfPage:
        if not 0 <= index < self.page_count:
            raise IndexError(f"page {index} out of range (0..{self.page_count - 1})")
        source = self._pages[index].source
        page = self._page_cache.get(source)
        if page is None:
            page = self._pdf[source]
            self._page_cache[source] = page
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
            may_draw_forms=self._has_forms,
            prefer_bgrx=True,  # 32-bit aligned rows blit faster into Qt
        )
