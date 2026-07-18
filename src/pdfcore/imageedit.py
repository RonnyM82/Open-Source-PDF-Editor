"""Image operations: list, hit-test, insert, replace-in-place (Phase 2, E6).

Pure PyMuPDF; no Qt. All coordinates are unrotated page points — the same
contract as textedit (get_image_info shares the text-extraction machinery, so
its bboxes live in the same space as span bboxes).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf


@dataclass(frozen=True)
class ImageInfo:
    """One placed image: its object xref and bbox in unrotated page points."""

    xref: int
    bbox: tuple[float, float, float, float]


def images_on_page(doc: pymupdf.Document, page_index: int) -> list[ImageInfo]:
    """Every image placement on the page."""
    return [
        ImageInfo(xref=int(info.get("xref", 0)), bbox=tuple(info["bbox"]))
        for info in doc[page_index].get_image_info(xrefs=True)
    ]


def image_at(doc: pymupdf.Document, page_index: int, px: float, py: float) -> ImageInfo | None:
    """The image under a page point (smallest bbox area wins), or None."""
    best = None
    best_area = float("inf")
    for info in images_on_page(doc, page_index):
        x0, y0, x1, y1 = info.bbox
        if x0 <= px <= x1 and y0 <= py <= y1:
            area = (x1 - x0) * (y1 - y0)
            if area < best_area:
                best, best_area = info, area
    return best


def insert_image(
    doc: pymupdf.Document,
    page_index: int,
    rect: tuple[float, float, float, float],
    image_path: str | Path,
) -> None:
    """Place an image file into ``rect`` (additive; aspect preserved).

    The bitmap is drawn in unrotated page space, so on a rotated page it must
    be counter-rotated to appear upright to the viewer (review finding: a
    one-click insert on a rot-90 page came out sideways).
    """
    path = Path(image_path)
    if not path.is_file():
        raise ValueError(f"image file not found: {path}")
    target = pymupdf.Rect(rect)
    if target.is_empty or target.is_infinite:
        raise ValueError("image rectangle is empty")
    page = doc[page_index]
    page.insert_image(target, filename=str(path), rotate=page.rotation % 360)


def _remove_image(
    doc: pymupdf.Document,
    page_index: int,
    target: ImageInfo,
    verb: str,
    moved: tuple[tuple[float, float, float, float], float, float] | None = None,
) -> None:
    """Redact ``target`` out — line-art/text preserved, other images spared.

    `IMAGE_REMOVE` deletes EVERY image overlapping the (slightly inset) rect
    in its entirety (review finding), so the op is REFUSED when another image
    genuinely overlaps the target rather than silently destroying it. ``verb``
    is used in that refusal message ("replacing"/"moving"/"deleting").
    Review comments intersecting the redaction are preserved (and, for a
    MOVE, callout targets on the image follow it) via the comment guard.
    """
    from pdfcore import comments as comments_module

    page = doc[page_index]
    x0, y0, x1, y1 = target.bbox
    inset = min(0.5, (x1 - x0) / 4, (y1 - y0) / 4)
    redact_rect = pymupdf.Rect(x0 + inset, y0 + inset, x1 - inset, y1 - inset)
    for other in images_on_page(doc, page_index):
        if other.bbox == target.bbox:
            continue
        if redact_rect.intersects(pymupdf.Rect(other.bbox)):
            raise ValueError(f"Another image overlaps this one — {verb} it would destroy both.")
    comment_guard = comments_module.guard(doc, page_index, moved=moved)
    page.add_redact_annot(redact_rect)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_NONE,
    )
    comment_guard.restore()


def replace_image(
    doc: pymupdf.Document,
    page_index: int,
    target: ImageInfo,
    image_path: str | Path,
) -> None:
    """Swap the image at ``target`` for a new file, in the same rectangle.

    Line-art and text crossing the image survive (borders/captions).
    Destructive at the PDF level; undo restores snapshots as everywhere else.
    """
    path = Path(image_path)
    if not path.is_file():
        raise ValueError(f"image file not found: {path}")
    _remove_image(doc, page_index, target, "replacing")
    page = doc[page_index]
    page.insert_image(pymupdf.Rect(target.bbox), filename=str(path), rotate=page.rotation % 360)


def delete_image(doc: pymupdf.Document, page_index: int, target: ImageInfo) -> None:
    """Remove the image at ``target`` (redact out; nothing reinserted)."""
    _remove_image(doc, page_index, target, "deleting")


# Smallest side an image may be resized to (points).
_MIN_IMAGE_SIDE = 6.0


def _placement_rotation(page: pymupdf.Page, target: ImageInfo) -> int:
    """Current rotation (0/90/180/270) of the placement, from its transform.

    Sign pattern probe-verified on 1.28.0 (y-down page space): rotate=0 →
    ``a>0``; 90 → ``b<0``; 180 → ``a<0``; 270 → ``b>0``. Reading it back is
    what makes repeated rotations COMPOUND, and lets move/resize PRESERVE a
    user rotation instead of resetting it to the page default.
    """
    for info in page.get_image_info(xrefs=True):
        if int(info.get("xref", 0)) == target.xref and tuple(info["bbox"]) == target.bbox:
            a, b = info["transform"][0], info["transform"][1]
            if abs(a) >= abs(b):
                return 0 if a >= 0 else 180
            return 90 if b < 0 else 270
    return page.rotation % 360  # placement not found: page-upright fallback


def _reinsert_image(
    doc: pymupdf.Document,
    page_index: int,
    target: ImageInfo,
    new_rect: tuple[float, float, float, float],
    verb: str,
    rotate: int | None = None,
) -> None:
    """Redact the target's current placement and re-draw the SAME image
    object (by xref) at ``new_rect``, clamped onto the page.

    Reusing the xref is lossless AND preserves the soft mask (transparency):
    ``extract_image`` returns only the base colour stream, so re-inserting
    that turns a transparent PNG solid black (the signature-goes-black bug).
    The image object survives ``apply_redactions`` (which drops the placement,
    not the object), so inserting it by xref afterwards works. ``rotate``
    None preserves the placement's current rotation (read back from its
    transform BEFORE the redaction removes it).
    """
    if target.xref <= 0:
        raise ValueError("this image can't be repositioned (no reusable reference)")
    page = doc[page_index]
    if rotate is None:
        rotate = _placement_rotation(page, target)
    x0, y0, x1, y1 = new_rect
    w, h = x1 - x0, y1 - y0
    page_w, page_h = page.rect.width, page.rect.height
    if page.rotation % 180 == 90:  # unrotated bounds (the space we insert in)
        page_w, page_h = page_h, page_w
    w, h = min(w, page_w), min(h, page_h)
    nx0 = min(max(x0, 0.0), max(0.0, page_w - w))
    ny0 = min(max(y0, 0.0), max(0.0, page_h - h))
    rect = pymupdf.Rect(nx0, ny0, nx0 + w, ny0 + h)

    # A pure translation (same size) carries callout targets on the image
    # along with it; resize/rotate only preserve the comments.
    ox0, oy0, ox1, oy1 = target.bbox
    moved = None
    if abs((ox1 - ox0) - w) < 0.5 and abs((oy1 - oy0) - h) < 0.5:
        dx, dy = nx0 - ox0, ny0 - oy0
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            moved = (target.bbox, dx, dy)
    _remove_image(doc, page_index, target, verb, moved=moved)
    page.insert_image(rect, xref=target.xref, rotate=rotate % 360)


def rotate_image(
    doc: pymupdf.Document,
    page_index: int,
    target: ImageInfo,
    deg: int,
) -> None:
    """Rotate the image placement by ``deg`` (±90) about its rect centre.

    ``insert_image``'s rotate turns COUNTER-clockwise in viewed page space
    (probe-verified: rotate=90 moves the image's top to the left), so the UI
    maps clockwise to −90. The rect swaps width/height about its centre —
    re-inserting a turned image into the unswapped rect letterboxes it —
    and the current placement rotation is read back from the transform so
    repeated rotations compound. Lossless (same xref, mask intact).
    """
    if deg % 360 not in (90, 270):
        raise ValueError("image rotation must be ±90 degrees")
    page = doc[page_index]
    current = _placement_rotation(page, target)
    x0, y0, x1, y1 = target.bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w, half_h = (y1 - y0) / 2, (x1 - x0) / 2  # swapped about the centre
    new_rect = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
    _reinsert_image(doc, page_index, target, new_rect, "rotating", rotate=(current + deg) % 360)


def move_image(
    doc: pymupdf.Document,
    page_index: int,
    target: ImageInfo,
    offset: tuple[float, float],
) -> None:
    """Move the image at ``target`` by ``offset`` (unrotated page points).

    Lossless — the same image object is re-placed at the translated rect
    (same size, transparency intact), clamped onto the page.
    """
    x0, y0, x1, y1 = target.bbox
    _reinsert_image(
        doc,
        page_index,
        target,
        (x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]),
        "moving",
    )


def resize_image(
    doc: pymupdf.Document,
    page_index: int,
    target: ImageInfo,
    new_rect: tuple[float, float, float, float],
) -> None:
    """Resize the image at ``target`` to ``new_rect`` (unrotated page points).

    The same image object is re-placed at the new rectangle (transparency
    intact); ``insert_image`` keeps the image's proportion within it. The rect
    is normalized and must be at least ``_MIN_IMAGE_SIDE`` on each side.
    """
    x0, y0, x1, y1 = new_rect
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if (x1 - x0) < _MIN_IMAGE_SIDE or (y1 - y0) < _MIN_IMAGE_SIDE:
        raise ValueError("that image size is too small")
    _reinsert_image(doc, page_index, target, (x0, y0, x1, y1), "resizing")
