"""Pure coordinate seam between rendered pixels and PDF page space (Phase 2).

No Qt and no PyMuPDF at runtime (the cross-validation against PyMuPDF's
rotation matrices lives in tests) — mirrors how ``compute_target_rect`` was
isolated for printing. The ONLY rotation-aware code in the app lives here; the
engine is rotation-blind (everything in pdfcore speaks unrotated page space).

Spaces:

- **Page space** — unrotated PDF points, origin top-left, y down. What
  ``get_text("dict")`` bboxes/origins use and what ``add_redact_annot`` /
  ``insert_text`` expect.
- **Scene space** — pixel coordinates of the rendered pixmap in the
  QGraphicsScene (pixmap item at the origin). ``get_pixmap`` applies the page
  rotation and scales by ``render_zoom``, so scene = rotated page points ×
  ``render_zoom`` (pixmap dimensions swap at 90/270).
- Viewport space stays Qt's business (``mapToScene`` / ``mapFromScene``) and
  never enters this module.

``page_size_pts`` is always the ROTATION-APPLIED size — exactly what
``PdfDocument.page_size(n)`` (``page.rect``) returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing only — runtime stays pymupdf-free
    from pdfcore.textedit import TextSpan


def _unrotated_size(rotation: int, page_size_pts: tuple[float, float]) -> tuple[float, float]:
    if rotation % 180 == 90:
        return (page_size_pts[1], page_size_pts[0])
    return page_size_pts


def page_to_scene(
    px: float,
    py: float,
    *,
    render_zoom: float,
    rotation: int,
    page_size_pts: tuple[float, float],
) -> tuple[float, float]:
    """Unrotated page point -> rendered-pixmap pixel."""
    rotation %= 360
    w, h = _unrotated_size(rotation, page_size_pts)
    if rotation == 0:
        rx, ry = px, py
    elif rotation == 90:
        rx, ry = h - py, px
    elif rotation == 180:
        rx, ry = w - px, h - py
    elif rotation == 270:
        rx, ry = py, w - px
    else:
        raise ValueError(f"rotation must be a multiple of 90, got {rotation}")
    return (rx * render_zoom, ry * render_zoom)


def scene_to_page(
    sx: float,
    sy: float,
    *,
    render_zoom: float,
    rotation: int,
    page_size_pts: tuple[float, float],
) -> tuple[float, float]:
    """Rendered-pixmap pixel -> unrotated page point."""
    rotation %= 360
    w, h = _unrotated_size(rotation, page_size_pts)
    rx, ry = sx / render_zoom, sy / render_zoom
    if rotation == 0:
        return (rx, ry)
    if rotation == 90:
        return (ry, h - rx)
    if rotation == 180:
        return (w - rx, h - ry)
    if rotation == 270:
        return (w - ry, rx)
    raise ValueError(f"rotation must be a multiple of 90, got {rotation}")


def page_rect_to_scene(
    bbox: tuple[float, float, float, float],
    *,
    render_zoom: float,
    rotation: int,
    page_size_pts: tuple[float, float],
) -> tuple[float, float, float, float]:
    """Unrotated page rect -> normalized scene rect ``(x0, y0, x1, y1)``."""
    x0, y0, x1, y1 = bbox
    ax, ay = page_to_scene(
        x0, y0, render_zoom=render_zoom, rotation=rotation, page_size_pts=page_size_pts
    )
    bx, by = page_to_scene(
        x1, y1, render_zoom=render_zoom, rotation=rotation, page_size_pts=page_size_pts
    )
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def span_at(spans: Sequence[TextSpan], px: float, py: float, pad: float = 1.0) -> TextSpan | None:
    """The span whose bbox contains the page point (± ``pad``); smallest wins.

    Overlapping bboxes happen (kerned/italic neighbours); preferring the
    smallest area picks the tightest — most specific — span under the click.
    """
    best = None
    best_area = float("inf")
    for span in spans:
        x0, y0, x1, y1 = span.bbox
        if x0 - pad <= px <= x1 + pad and y0 - pad <= py <= y1 + pad:
            area = (x1 - x0) * (y1 - y0)
            if area < best_area:
                best, best_area = span, area
    return best
