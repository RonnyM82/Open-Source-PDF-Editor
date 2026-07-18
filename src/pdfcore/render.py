"""Rasterizing pages to plain pixel buffers (no Qt types).

RenderedPage carries the raw bytes; pdfapp/qt_image.py is the only place that
turns them into a QImage. Keeping the raster as a plain dataclass is what lets
the engine stay headless and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf


@dataclass(frozen=True)
class RenderedPage:
    """A rasterized page as a raw byte buffer.

    - ``width`` / ``height``: pixel dimensions.
    - ``stride``: bytes per row (>= ``width * channels``; MuPDF rows are unpadded
      in practice, but callers should use ``stride``, not assume ``width*n``).
    - ``samples``: raw pixel bytes, ``len == stride * height``.
    - ``alpha``: whether an alpha channel is present.
    - ``channels``: components per pixel — 1 (grayscale), 3 (RGB), 4 (RGBA).
      The qt_image seam maps this to the QImage format.
    """

    width: int
    height: int
    stride: int
    samples: bytes
    alpha: bool
    channels: int


def render_page(
    page: pymupdf.Page,
    *,
    zoom: float = 1.0,
    dpi: int | None = None,
    alpha: bool = False,
) -> RenderedPage:
    """Render a PyMuPDF page to a :class:`RenderedPage`.

    ``dpi``, when given, takes precedence over ``zoom``. ``zoom=1.0`` renders at
    the PDF's native 72 dpi.
    """
    if dpi is not None:
        pix = page.get_pixmap(dpi=dpi, alpha=alpha)
    else:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=alpha)
    return _to_rendered_page(pix)


def render_page_at_dpi(page: pymupdf.Page, dpi: int, *, gray: bool = False) -> RenderedPage:
    """Render a page at an explicit DPI, optionally greyscale.

    Stateless and pure — no caching (any render cache lives in the UI layer).
    ``gray=True`` renders in the grayscale colorspace (1 byte/pixel), which the
    seam maps to ``QImage.Format_Grayscale8``.
    """
    colorspace = pymupdf.csGRAY if gray else pymupdf.csRGB
    pix = page.get_pixmap(dpi=dpi, colorspace=colorspace, alpha=False)
    return _to_rendered_page(pix)


def _to_rendered_page(pix: pymupdf.Pixmap) -> RenderedPage:
    return RenderedPage(
        width=pix.width,
        height=pix.height,
        stride=pix.stride,
        samples=pix.samples,
        alpha=bool(pix.alpha),
        channels=pix.n,
    )
