"""Tests for page rendering (pdfcore.render / PdfDocument.render_page)."""

from __future__ import annotations

from pdfcore.document import PdfDocument
from pdfcore.render import RenderedPage


def test_render_page_shape_and_buffer(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        rp = doc.render_page(0, zoom=2.0)
    assert isinstance(rp, RenderedPage)
    assert rp.width > 0 and rp.height > 0
    assert rp.alpha is False
    assert rp.channels == 3
    assert rp.stride >= rp.width * rp.channels
    assert len(rp.samples) == rp.stride * rp.height


def test_zoom_scales_pixel_dimensions(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        small = doc.render_page(0, zoom=1.0)
        big = doc.render_page(0, zoom=2.0)
    assert big.width > small.width
    assert big.height > small.height


def test_render_by_dpi(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        low = doc.render_page(0, dpi=72)
        high = doc.render_page(0, dpi=200)
    assert high.width > low.width and high.height > low.height


def test_render_with_alpha_is_rgba(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        rp = doc.render_page(0, alpha=True)
    assert rp.alpha is True
    assert rp.channels == 4
    assert len(rp.samples) == rp.stride * rp.height


# --- B1: render_page_at_dpi (stateless engine method) -------------------


def test_render_at_dpi_rgb_dimensions(text_pdf):
    # text_pdf pages are A4 (595x842 pt). At 300 dpi -> ~2480x3508 px, RGB.
    with PdfDocument.open(text_pdf) as doc:
        rp = doc.render_page_at_dpi(0, 300)
    assert rp.channels == 3
    assert rp.alpha is False
    assert 2470 <= rp.width <= 2490
    assert 3500 <= rp.height <= 3515
    assert len(rp.samples) == rp.stride * rp.height


def test_render_at_dpi_gray_is_single_channel(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        rgb = doc.render_page_at_dpi(0, 150)
        gray = doc.render_page_at_dpi(0, 150, gray=True)
    assert rgb.channels == 3
    assert gray.channels == 1
    assert gray.alpha is False
    assert len(gray.samples) == gray.stride * gray.height
    # Grayscale is ~1/3 the bytes of RGB at the same dpi (definitely smaller).
    assert len(gray.samples) < len(rgb.samples)
