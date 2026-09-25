"""Coordinate conversion between PDF user space, bitmap space, and widget space.

Three coordinate systems, and every bug in a PDF editor lives in the seams
between them:

  PDF user space   origin bottom-left, +y up, units = points (1/72 in).
                   The page box may not start at (0, 0).
  Bitmap space     origin top-left, +y down, units = pixels. Already rotated
                   and scaled. This is what pdfium hands back.
  Widget space     origin top-left, +y down, logical pixels. Bitmap space
                   translated by wherever the page's top-left corner sits
                   inside the widget.

Rotation is TOTAL clockwise rotation: the page's intrinsic /Rotate plus any
view rotation the user applied. Scale is device pixels per point.

Everything here is pure and side-effect free. It is validated against
pdfium's own PdfPosConv in tests/test_transform.py -- if this file and pdfium
ever disagree, the tests fail loudly rather than producing annotations that
land two inches from where the user clicked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

Point = Tuple[float, float]
Rect = Tuple[float, float, float, float]

VALID_ROTATIONS = (0, 90, 180, 270)


def normalize_rotation(degrees: float) -> int:
    """Fold an arbitrary rotation into {0, 90, 180, 270}.

    PDF /Rotate is allowed to be negative or greater than 360, so this is not
    paranoia -- real files do it.
    """
    deg = int(round(degrees))
    if deg % 90 != 0:
        raise ValueError(f"rotation must be a multiple of 90, got {degrees!r}")
    return deg % 360


def normalize_rect(rect: Rect) -> Rect:
    """Return (x0, y0, x1, y1) with x0 <= x1 and y0 <= y1."""
    x0, y0, x1, y1 = rect
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


@dataclass(frozen=True)
class PageTransform:
    """Immutable mapping between the three coordinate systems for one page.

    Args:
        box: the page box in PDF user space, unrotated, as (x0, y0, x1, y1).
            Normally the CropBox. Not required to start at the origin.
        rotation: total clockwise rotation in degrees -- the page's intrinsic
            /Rotate plus any view rotation. Must be a multiple of 90.
        scale: device pixels per PDF point. 1.0 == 72 DPI.
        offset: where the rendered page's top-left corner sits in widget
            space, in the same pixel units as `scale` produces.
    """

    box: Rect
    rotation: int = 0
    scale: float = 1.0
    offset: Point = (0.0, 0.0)

    def __post_init__(self) -> None:
        object.__setattr__(self, "box", normalize_rect(self.box))
        object.__setattr__(self, "rotation", normalize_rotation(self.rotation))
        if self.scale <= 0:
            raise ValueError(f"scale must be positive, got {self.scale!r}")
        object.__setattr__(self, "scale", float(self.scale))
        object.__setattr__(self, "offset", (float(self.offset[0]), float(self.offset[1])))
        if self.box_width <= 0 or self.box_height <= 0:
            raise ValueError(f"degenerate page box: {self.box!r}")

    # ---- geometry -----------------------------------------------------

    @property
    def box_width(self) -> float:
        """Unrotated page width in points."""
        return self.box[2] - self.box[0]

    @property
    def box_height(self) -> float:
        """Unrotated page height in points."""
        return self.box[3] - self.box[1]

    @property
    def display_size_pt(self) -> Point:
        """Page size in points as displayed, i.e. after rotation."""
        if self.rotation in (90, 270):
            return (self.box_height, self.box_width)
        return (self.box_width, self.box_height)

    @property
    def bitmap_size(self) -> Tuple[int, int]:
        """Rendered bitmap size in pixels.

        Uses ceil to match pdfium, which is what pypdfium2 does internally.
        Verified against the real library, not assumed.
        """
        w, h = self.display_size_pt
        return (math.ceil(w * self.scale), math.ceil(h * self.scale))

    # ---- point conversion ---------------------------------------------

    def pdf_to_bitmap(self, x: float, y: float) -> Point:
        """PDF user space -> bitmap pixel space."""
        w, h = self.box_width, self.box_height
        # Normalise to page-local, then flip to a y-down origin.
        a = x - self.box[0]
        b = self.box[3] - y

        rot = self.rotation
        if rot == 0:
            ra, rb = a, b
        elif rot == 90:
            ra, rb = h - b, a
        elif rot == 180:
            ra, rb = w - a, h - b
        else:  # 270
            ra, rb = b, w - a

        return (ra * self.scale, rb * self.scale)

    def bitmap_to_pdf(self, bx: float, by: float) -> Point:
        """Bitmap pixel space -> PDF user space."""
        w, h = self.box_width, self.box_height
        ra = bx / self.scale
        rb = by / self.scale

        rot = self.rotation
        if rot == 0:
            a, b = ra, rb
        elif rot == 90:
            a, b = rb, h - ra
        elif rot == 180:
            a, b = w - ra, h - rb
        else:  # 270
            a, b = w - rb, ra

        return (a + self.box[0], self.box[3] - b)

    def pdf_to_widget(self, x: float, y: float) -> Point:
        bx, by = self.pdf_to_bitmap(x, y)
        return (bx + self.offset[0], by + self.offset[1])

    def widget_to_pdf(self, wx: float, wy: float) -> Point:
        return self.bitmap_to_pdf(wx - self.offset[0], wy - self.offset[1])

    # ---- rect conversion ----------------------------------------------

    def pdf_rect_to_widget(self, rect: Rect) -> Rect:
        """Convert a PDF-space rect to a normalised widget-space rect.

        Rotation reorders corners, so the result is renormalised. Never
        assume the transformed x0 is still the left edge.
        """
        x0, y0, x1, y1 = normalize_rect(rect)
        p0 = self.pdf_to_widget(x0, y0)
        p1 = self.pdf_to_widget(x1, y1)
        return normalize_rect((p0[0], p0[1], p1[0], p1[1]))

    def widget_rect_to_pdf(self, rect: Rect) -> Rect:
        x0, y0, x1, y1 = normalize_rect(rect)
        p0 = self.widget_to_pdf(x0, y0)
        p1 = self.widget_to_pdf(x1, y1)
        return normalize_rect((p0[0], p0[1], p1[0], p1[1]))

    # ---- scalar conversion ---------------------------------------------

    def points_to_pixels(self, length: float) -> float:
        """Convert a rotation-independent length (line width, font size)."""
        return length * self.scale

    def pixels_to_points(self, length: float) -> float:
        return length / self.scale

    def contains_widget_point(self, wx: float, wy: float) -> bool:
        bw, bh = self.bitmap_size
        ox, oy = self.offset
        return ox <= wx <= ox + bw and oy <= wy <= oy + bh

    # ---- derivation ----------------------------------------------------

    def with_scale(self, scale: float) -> "PageTransform":
        return PageTransform(self.box, self.rotation, scale, self.offset)

    def with_rotation(self, rotation: int) -> "PageTransform":
        return PageTransform(self.box, rotation, self.scale, self.offset)

    def rotated_by(self, delta: int) -> "PageTransform":
        return self.with_rotation(self.rotation + delta)

    def with_offset(self, offset: Point) -> "PageTransform":
        return PageTransform(self.box, self.rotation, self.scale, offset)

    # ---- fitting -------------------------------------------------------

    def scale_to_fit_width(self, available_px: float, margin_px: float = 0.0) -> float:
        w, _ = self.display_size_pt
        return max((available_px - margin_px) / w, 0.01)

    def scale_to_fit_page(
        self, available_w_px: float, available_h_px: float, margin_px: float = 0.0
    ) -> float:
        w, h = self.display_size_pt
        return max(
            min((available_w_px - margin_px) / w, (available_h_px - margin_px) / h), 0.01
        )
