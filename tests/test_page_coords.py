"""Pure tests for the coordinate seam + span hit-testing.

The module under test must stay Qt-free AND pymupdf-free at RUNTIME (only this
test file may import pymupdf, for cross-validation against the real rotation
matrices) — asserted by the subprocess import test below.
"""

from __future__ import annotations

import subprocess
import sys
from collections import namedtuple

import pymupdf
import pytest

from pdfapp.page_coords import page_rect_to_scene, page_to_scene, scene_to_page, span_at

PAGE = (595.0, 842.0)  # unrotated A4 portrait, points
POINTS = [(0.0, 0.0), (100.0, 200.0), (595.0, 842.0), (12.5, 800.25)]


def _rotated_size(rotation: int) -> tuple[float, float]:
    return (PAGE[1], PAGE[0]) if rotation % 180 == 90 else PAGE


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("zoom", [0.5, 1.0, 3.7])
def test_scene_page_roundtrip(rotation, zoom):
    size = _rotated_size(rotation)
    for px, py in POINTS:
        sx, sy = page_to_scene(px, py, render_zoom=zoom, rotation=rotation, page_size_pts=size)
        back = scene_to_page(sx, sy, render_zoom=zoom, rotation=rotation, page_size_pts=size)
        assert back == pytest.approx((px, py), abs=1e-9)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 450, -90])
def test_cross_validate_with_pymupdf_matrices(rotation):
    """The seam must agree exactly with PyMuPDF's own rotation matrices."""
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=PAGE[0], height=PAGE[1])
        page.set_rotation(rotation % 360)
        size = (page.rect.width, page.rect.height)  # rotation-applied, like page_size(n)
        for px, py in POINTS:
            expected = pymupdf.Point(px, py) * page.rotation_matrix
            got = page_to_scene(px, py, render_zoom=1.0, rotation=rotation, page_size_pts=size)
            assert got == pytest.approx((expected.x, expected.y), abs=1e-6)

            back = pymupdf.Point(expected.x, expected.y) * page.derotation_matrix
            inv = scene_to_page(
                expected.x, expected.y, render_zoom=1.0, rotation=rotation, page_size_pts=size
            )
            assert inv == pytest.approx((back.x, back.y), abs=1e-6)
    finally:
        doc.close()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_rect_to_scene_is_normalized(rotation):
    size = _rotated_size(rotation)
    x0, y0, x1, y1 = page_rect_to_scene(
        (100.0, 200.0, 160.0, 212.0), render_zoom=2.0, rotation=rotation, page_size_pts=size
    )
    assert x0 < x1 and y0 < y1
    # Area is rotation-invariant: (60 x 12 pts) at zoom 2 -> 120 x 24 px.
    assert {round(x1 - x0), round(y1 - y0)} == {120, 24}


def test_rejects_non_multiple_of_90():
    with pytest.raises(ValueError):
        page_to_scene(0, 0, render_zoom=1.0, rotation=45, page_size_pts=PAGE)


# --- span hit-testing -------------------------------------------------------

FakeSpan = namedtuple("FakeSpan", ["bbox"])  # duck-typed: span_at needs only .bbox


def test_span_at_hit_miss_and_smallest_wins():
    small = FakeSpan((10.0, 10.0, 50.0, 20.0))
    large = FakeSpan((5.0, 5.0, 100.0, 40.0))  # overlaps small entirely
    assert span_at([large, small], 15, 15) is small  # smallest area wins
    assert span_at([small, large], 15, 15) is small  # order-independent
    assert span_at([small, large], 90, 35) is large  # only large contains it
    assert span_at([small], 50.8, 15) is small  # within default pad (1.0)
    assert span_at([small], 52.5, 15) is None  # beyond pad
    assert span_at([small], 15, 15, pad=0.0) is small
    assert span_at([], 0, 0) is None


# --- runtime purity ---------------------------------------------------------


def test_module_imports_no_qt_or_pymupdf():
    """page_coords must not pull Qt or pymupdf into the process at import."""
    code = (
        "import sys, pdfapp.page_coords; "
        "bad = [m for m in ('pymupdf', 'PySide6') "
        "if any(k == m or k.startswith(m + '.') for k in sys.modules)]; "
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
