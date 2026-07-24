"""Per-page cache of editable geometry (UI-side; the engine stays stateless).

Foundation for hover hit-testing (U2a) and the reveal-all outlines (U5):
every span, paragraph and image placement of a page, held in PAGE space
(unrotated points). Zoom and rotation apply at paint/hit time through
``page_coords``, so entries stay valid across re-renders.

Invalidation mirrors the render cache (CLAUDE.md rule 8) through the same
``DocumentView.after_command`` funnel: page-scoped ops evict that page;
structural ops and every undo/redo restore clear everything (so does a
save-as reopen — ``garbage=4`` can renumber xrefs, which stales ImageInfo).
"""

from __future__ import annotations

from dataclasses import dataclass

from pdfcore.comments import CommentInfo
from pdfcore.document import PdfDocument
from pdfcore.imageedit import ImageInfo
from pdfcore.links import LinkInfo
from pdfcore.textedit import Paragraph, TextSpan


@dataclass(frozen=True)
class PageGeometry:
    """Everything editable on one page, in unrotated page points."""

    spans: tuple[TextSpan, ...]
    paragraphs: tuple[Paragraph, ...]
    images: tuple[ImageInfo, ...]
    comments: tuple[CommentInfo, ...] = ()
    links: tuple[LinkInfo, ...] = ()


def collect_geometry(doc: PdfDocument, n: int, boundaries: tuple = ()) -> PageGeometry:
    """Extract page ``n``'s editable geometry.

    ``boundaries`` (registered insert boxes, each ``(rect, fingerprint)``)
    isolate inserted boxes so they are their own paragraphs, never merged with
    pre-existing lines — content-aware so overlapping boxes don't cross-assign.
    """
    return PageGeometry(
        spans=tuple(doc.text_spans(n)),
        paragraphs=tuple(doc.paragraphs(n, boundaries=boundaries)),
        images=tuple(doc.images(n)),
        comments=tuple(doc.comments(n)),
        links=tuple(doc.links(n)),
    )


@dataclass(frozen=True)
class HoverTarget:
    """What sits under a hovered page point (U2a) — page-space geometry.

    ``corner`` is the GRABBED corner (the one under the cursor); the resize
    op anchors at the opposite one. ``corner_zone`` is the grab-zone size in
    page points (drawn as corner ticks on image hover). ``payload`` is the
    underlying Paragraph or ImageInfo (U6 selection stores it).
    """

    kind: str  # "text" | "image" | "image_corner" | "comment" | "link_move" | "link_corner"
    bbox: tuple[float, float, float, float]
    corner: tuple[float, float] | None = None
    corner_zone: float = 0.0
    payload: object | None = None


# Corner grab zone for image resize — THE rule, shared by hover cursors and
# the Ctrl+drag resize routing (document_view). Within min(18pt, w/3, h/3)
# of a corner counts as grabbing that corner.
CORNER_ZONE_PT = 18.0


def corner_zone(bbox: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = bbox
    return min(CORNER_ZONE_PT, (x1 - x0) / 3, (y1 - y0) / 3)


def corner_hit(
    bbox: tuple[float, float, float, float], px: float, py: float
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """``(grabbed_corner, opposite_anchor)`` for a point in a corner zone."""
    x0, y0, x1, y1 = bbox
    zone = corner_zone(bbox)
    pairs = (
        ((x0, y0), (x1, y1)),
        ((x1, y0), (x0, y1)),
        ((x0, y1), (x1, y0)),
        ((x1, y1), (x0, y0)),
    )
    for grabbed, opposite in pairs:
        if abs(px - grabbed[0]) <= zone and abs(py - grabbed[1]) <= zone:
            return grabbed, opposite
    return None


def _contains(
    bbox: tuple[float, float, float, float], px: float, py: float, pad: float = 0.0
) -> bool:
    x0, y0, x1, y1 = bbox
    return x0 - pad <= px <= x1 + pad and y0 - pad <= py <= y1 + pad


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def rect_encloses(
    outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]
) -> bool:
    """True when ``inner`` sits entirely within ``outer`` (window marquee)."""
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def rect_intersects(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """True when the two axis-aligned rects overlap at all (crossing marquee)."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def hover_target(
    geometry: PageGeometry, px: float, py: float, pad: float = 1.0
) -> HoverTarget | None:
    """The editing affordance under a page point, or None.

    COMMENTS win over everything (they are markup floating on top — E11);
    then text over images (the priority the Ctrl+drag routing already
    uses); the smallest box wins within a kind (same rule as
    ``span_at``/``image_at``). Text hovers outline the PARAGRAPH — the
    stable editable block — not the individual span.
    """
    comment = min(
        (c for c in geometry.comments if _contains(c.rect, px, py)),
        key=lambda c: _area(c.rect),
        default=None,
    )
    if comment is not None:
        return HoverTarget("comment", comment.rect, payload=comment)
    para = min(
        (p for p in geometry.paragraphs if _contains(p.bbox, px, py, pad)),
        key=lambda p: _area(p.bbox),
        default=None,
    )
    if para is not None:
        return HoverTarget("text", para.bbox, payload=para)
    image = min(
        (i for i in geometry.images if _contains(i.bbox, px, py)),
        key=lambda i: _area(i.bbox),
        default=None,
    )
    if image is None:
        return _link_target(geometry, px, py)
    hit = corner_hit(image.bbox, px, py)
    if hit is not None:
        return HoverTarget(
            "image_corner",
            image.bbox,
            corner=hit[0],
            corner_zone=corner_zone(image.bbox),
            payload=image,
        )
    return HoverTarget("image", image.bbox, corner_zone=corner_zone(image.bbox), payload=image)


def _link_target(geometry: PageGeometry, px: float, py: float) -> HoverTarget | None:
    """A link hotspot under the point — the LOWEST hover priority.

    Because links usually sit over text (which wins above), a link is only the
    hover target where nothing else covers it — so grabbing a link never steals
    a text/image edit gesture. Links buried under text are edited via the
    right-click menu instead, and follow the text when it moves (the engine's
    link guard). A press near a corner RESIZES (``link_corner``); the body
    MOVES (``link_move``).
    """
    link = min(
        (lk for lk in geometry.links if _contains(lk.bbox, px, py)),
        key=lambda lk: _area(lk.bbox),
        default=None,
    )
    if link is None:
        return None
    hit = corner_hit(link.bbox, px, py)
    if hit is not None:
        return HoverTarget(
            "link_corner",
            link.bbox,
            corner=hit[0],
            corner_zone=corner_zone(link.bbox),
            payload=link,
        )
    return HoverTarget("link_move", link.bbox, corner_zone=corner_zone(link.bbox), payload=link)


def nearest_span_in_paragraph(para: Paragraph, px: float, py: float) -> TextSpan | None:
    """The span a click INSIDE the paragraph box most plausibly means.

    The displayed outline is the paragraph's union bbox, so a double-click
    on blank space inside it must still resolve a line target: pick the
    line whose y-range contains ``py`` (else the nearest line), then the
    span containing ``px`` (else the nearest by x).
    """
    lines = para.lines or ()
    if not lines:
        return None

    def line_distance(line: tuple[TextSpan, ...]) -> float:
        y0 = min(s.bbox[1] for s in line)
        y1 = max(s.bbox[3] for s in line)
        return 0.0 if y0 <= py <= y1 else min(abs(py - y0), abs(py - y1))

    line = min(lines, key=line_distance)

    def span_distance(span: TextSpan) -> float:
        x0, x1 = span.bbox[0], span.bbox[2]
        return 0.0 if x0 <= px <= x1 else min(abs(px - x0), abs(px - x1))

    return min(line, key=span_distance)


class GeometryCache:
    """Lazy per-page PageGeometry.

    The doc is passed at lookup time, not held: a DocumentView can swap its
    PdfDocument (save-as reopens the newly saved file) and a held reference
    would silently serve the old document's geometry.
    """

    def __init__(self) -> None:
        # n -> (geometry, boundaries it was built with) — rebuilt when the
        # caller's insert-isolation boundaries change.
        self._pages: dict[int, tuple[PageGeometry, tuple]] = {}

    def page(self, doc: PdfDocument, n: int, boundaries: tuple = ()) -> PageGeometry:
        boundaries = tuple(boundaries)
        entry = self._pages.get(n)
        if entry is None or entry[1] != boundaries:
            entry = (collect_geometry(doc, n, boundaries), boundaries)
            self._pages[n] = entry
        return entry[0]

    def evict_page(self, n: int) -> None:
        self._pages.pop(n, None)

    def clear(self) -> None:
        self._pages.clear()
