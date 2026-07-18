"""Geometry cache (U1): per-page editable geometry, PAGE space, UI-side.

The cache feeds hover hit-testing (U2a) and reveal-all outlines (U5); its
invalidation rides the same DocumentView.after_command funnel as the render
cache — page-scoped ops evict one page, structural ops / undo / save-as
clear everything.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_geometry import (  # noqa: E402
    GeometryCache,
    PageGeometry,
    collect_geometry,
    corner_hit,
    corner_zone,
    hover_target,
)
from pdfcore.document import PdfDocument  # noqa: E402
from pdfcore.imageedit import ImageInfo  # noqa: E402
from pdfcore.textedit import Paragraph  # noqa: E402


def _para(bbox):
    """A minimal synthetic Paragraph — hover resolution only reads bbox."""
    return Paragraph(
        page_index=0,
        text="p",
        bbox=bbox,
        first_origin=(bbox[0], bbox[3]),
        pitch=10.0,
        spans=(),
        font="Helvetica",
        base14="helv",
        size=10.0,
        color=0,
        flags=0,
        embedded=False,
        uniform_style=True,
    )


def test_collect_geometry_matches_engine(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        geometry = collect_geometry(doc, 0)
        assert geometry.spans == tuple(doc.text_spans(0))
        assert geometry.paragraphs == tuple(doc.paragraphs(0))
        assert geometry.images == tuple(doc.images(0))
        assert geometry.spans and geometry.paragraphs and geometry.images


def test_cache_reuses_until_invalidated(quote_pdf):
    cache = GeometryCache()
    with PdfDocument.open(quote_pdf.path) as doc:
        first = cache.page(doc, 0)
        assert cache.page(doc, 0) is first  # cached

        cache.evict_page(0)
        rebuilt = cache.page(doc, 0)
        assert rebuilt is not first
        assert rebuilt == first  # same content, fresh extraction

        cache.clear()
        assert cache.page(doc, 0) is not rebuilt


def test_corner_hit_zone_and_anchor():
    bbox = (0.0, 0.0, 90.0, 90.0)
    assert corner_zone(bbox) == 18.0
    grabbed, opposite = corner_hit(bbox, 5.0, 5.0)
    assert grabbed == (0.0, 0.0)
    assert opposite == (90.0, 90.0)
    grabbed, opposite = corner_hit(bbox, 88.0, 3.0)
    assert grabbed == (90.0, 0.0)
    assert opposite == (0.0, 90.0)
    assert corner_hit(bbox, 45.0, 45.0) is None
    # Small images shrink the zone (w/3) so the body stays grabbable.
    small = (0.0, 0.0, 12.0, 12.0)
    assert corner_zone(small) == 4.0
    assert corner_hit(small, 6.0, 6.0) is None


def test_hover_target_priorities_and_kinds():
    geometry = PageGeometry(
        spans=(),
        paragraphs=(_para((10.0, 10.0, 100.0, 30.0)),),
        images=(ImageInfo(xref=7, bbox=(50.0, 20.0, 150.0, 110.0)),),
    )
    # Text wins where the paragraph and image overlap (interaction priority).
    target = hover_target(geometry, 60.0, 25.0)
    assert target.kind == "text"
    assert target.bbox == (10.0, 10.0, 100.0, 30.0)
    # Image body, away from corners.
    target = hover_target(geometry, 100.0, 70.0)
    assert target.kind == "image"
    assert target.corner is None
    assert target.corner_zone == pytest.approx(18.0)
    # Image corner: grabbed corner reported (the resize anchors opposite).
    target = hover_target(geometry, 145.0, 105.0)
    assert target.kind == "image_corner"
    assert target.corner == (150.0, 110.0)
    # Nothing under an empty point.
    assert hover_target(geometry, 300.0, 300.0) is None


def test_hover_target_smallest_paragraph_wins():
    outer = _para((0.0, 0.0, 200.0, 200.0))
    inner = _para((40.0, 40.0, 80.0, 60.0))
    geometry = PageGeometry(spans=(), paragraphs=(outer, inner), images=())
    assert hover_target(geometry, 50.0, 50.0).bbox == inner.bbox


def test_hover_target_on_real_fixture(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        geometry = collect_geometry(doc, 0)
        logo = geometry.images[0]
        cx = (logo.bbox[0] + logo.bbox[2]) / 2
        cy = (logo.bbox[1] + logo.bbox[3]) / 2
        assert hover_target(geometry, cx, cy).kind == "image"
        span = next(s for s in geometry.spans if s.text.strip() == quote_pdf.price)
        target = hover_target(geometry, *span.origin)
        assert target is not None and target.kind == "text"


def test_view_invalidates_geometry_through_after_command(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)

        primed = view.page_geometry(0)
        assert view.page_geometry(0) is primed

        window.rotate_clockwise()  # page-scoped -> evict just this page
        after_rotate = view.page_geometry(0)
        assert after_rotate is not primed

        view.undo_stack.undo()  # restore -> clear everything
        assert view.page_geometry(0) is not after_rotate
    finally:
        window.close()
