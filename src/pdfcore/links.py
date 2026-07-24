"""PDF hyperlinks (link annotations): read, create, edit, delete, move/resize.

Pure PyMuPDF; no Qt. All PUBLIC coordinates are UNROTATED page points — the
same contract as textedit/imageedit/comments; the app-side ``page_coords`` seam
owns rotation.

Links are ``/Link`` annotations but PyMuPDF exposes them through a SEPARATE API
(``page.get_links()`` / ``insert_link`` / ``update_link`` / ``delete_link``),
NOT ``page.annots()``. There is no ``Annot`` object; identity is the annotation
``xref``.

Two probe-verified 1.28.0 facts drive this module:

* **Coordinate asymmetry.** ``insert_link``/``update_link`` interpret the
  ``from`` rect in UNROTATED page space (the stored ``/Rect`` is identical at
  rotation 0/90/180/270), but ``get_links()`` RETURNS ``from`` in ROTATED
  (viewed) space. So we DEROTATE on read (``× page.derotation_matrix``) and
  pass unrotated rects on write.
* **reload_page staleness.** After any mutation, ``get_links()`` on the live
  page — and even a fresh ``doc[n]`` — stays stale until
  ``doc.reload_page(page)``. The leaf mutations here refresh via
  :func:`_refresh` so the live document reflects the change immediately; the
  redaction guard cannot (its caller holds the page) and relies on the change
  surviving in the document bytes instead.

``apply_redactions`` deletes any link whose rect intersects a redaction band
(same as comments), so redaction-based edits bracket their redactions in
``guard(doc, n).restore()``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

# Readable kind names (mapped to pymupdf.LINK_* on the wire).
URI = "uri"
GOTO = "goto"
GOTOR = "gotor"
LAUNCH = "launch"
NAMED = "named"
UNKNOWN = "unknown"

_KIND_FROM_PYMUPDF: dict[int, str] = {
    pymupdf.LINK_URI: URI,
    pymupdf.LINK_GOTO: GOTO,
    pymupdf.LINK_GOTOR: GOTOR,
    pymupdf.LINK_LAUNCH: LAUNCH,
    pymupdf.LINK_NAMED: NAMED,
}
_KIND_TO_PYMUPDF: dict[str, int] = {URI: pymupdf.LINK_URI, GOTO: pymupdf.LINK_GOTO}

# Kinds this editor can CREATE and edit. Others are read/display/follow only.
EDITABLE_KINDS = (URI, GOTO)

# Smallest side a link rectangle may have (points).
_MIN_LINK_SIDE = 4.0

# The default "classic hyperlink" colour — Word's hyperlink blue (#0563C1),
# as an sRGB int for TextStyle.color and an (r, g, b) 0-1 tuple for drawn rules.
WORD_LINK_BLUE = 0x0563C1
WORD_LINK_BLUE_RGB = (0x05 / 255, 0x63 / 255, 0xC1 / 255)


@dataclass(frozen=True)
class LinkInfo:
    """One link hotspot, in UNROTATED page points.

    ``xref`` is the annotation identity. ``kind`` is a readable name. ``uri``
    is set for URI links; ``dest_page`` (0-based) and ``dest_point`` (unrotated
    point on the target page) for GOTO links. The rect is named ``bbox`` to
    match :class:`~pdfcore.imageedit.ImageInfo` so the UI selection chrome and
    hit-test helpers work unchanged.
    """

    page_index: int
    xref: int
    kind: str
    bbox: tuple[float, float, float, float]
    uri: str | None = None
    dest_page: int | None = None
    dest_point: tuple[float, float] | None = None

    @property
    def editable(self) -> bool:
        """True when this editor can change the link's target/rect (URI/GOTO)."""
        return self.kind in EDITABLE_KINDS


def _unrotate_rect(page: pymupdf.Page, from_rect: object) -> tuple[float, float, float, float]:
    """A get_links ``from`` (rotated/viewed space) → unrotated page-point tuple."""
    r = pymupdf.Rect(from_rect) * page.derotation_matrix
    r.normalize()
    return (r.x0, r.y0, r.x1, r.y1)


def _link_info(page_index: int, page: pymupdf.Page, lk: dict) -> LinkInfo:
    kind = _KIND_FROM_PYMUPDF.get(lk.get("kind"), UNKNOWN)
    bbox = _unrotate_rect(page, lk["from"])
    uri = lk.get("uri") if kind == URI else None
    dest_page: int | None = None
    dest_point: tuple[float, float] | None = None
    if kind == GOTO:
        page_no = lk.get("page")
        if isinstance(page_no, int) and page_no >= 0:
            dest_page = page_no
        to = lk.get("to")
        if to is not None and dest_page is not None and dest_page < page.parent.page_count:
            # `to` lives in the TARGET page's space — derotate with ITS matrix.
            p = pymupdf.Point(to) * page.parent[dest_page].derotation_matrix
            dest_point = (p.x, p.y)
    return LinkInfo(page_index, int(lk.get("xref", 0)), kind, bbox, uri, dest_page, dest_point)


def _refresh(doc: pymupdf.Document, page_index: int, page: pymupdf.Page) -> pymupdf.Page:
    """Return a page whose ``get_links()`` reflects a just-applied mutation.

    ``insert_link``/``update_link``/``delete_link`` do NOT invalidate the page's
    cached link list (probe-verified) — only ``reload_page`` (or a reopen) does.
    ``reload_page`` refuses to evict a page with more than one live Python
    reference, so this is only reliable for a LEAF mutation (no caller holding
    the page); it falls back to the stale cached page rather than raise. The
    redaction guard, whose caller always holds the page, deliberately does NOT
    call this (its re-inserted links still land in the document bytes).
    """
    try:
        return doc.reload_page(page)
    except Exception:  # noqa: BLE001 - a stray ref: fall back, the mutation still landed
        return doc[page_index]


def links_on_page(doc: pymupdf.Document, page_index: int) -> list[LinkInfo]:
    """Every link on the page, in unrotated page points."""
    page = doc[page_index]
    return [_link_info(page_index, page, lk) for lk in page.get_links()]


def link_at(doc: pymupdf.Document, page_index: int, px: float, py: float) -> LinkInfo | None:
    """The link under an unrotated page point (smallest rect area wins), or None."""
    best: LinkInfo | None = None
    best_area = float("inf")
    for info in links_on_page(doc, page_index):
        x0, y0, x1, y1 = info.bbox
        if x0 <= px <= x1 and y0 <= py <= y1:
            area = (x1 - x0) * (y1 - y0)
            if area < best_area:
                best, best_area = info, area
    return best


# --- creation / editing -------------------------------------------------------


def _page_bounds(page: pymupdf.Page) -> tuple[float, float]:
    """Unrotated (width, height) of the page — the space links are stored in."""
    w, h = page.rect.width, page.rect.height
    if page.rotation % 180 == 90:
        w, h = h, w
    return w, h


def _clamp_rect(
    page: pymupdf.Page, rect: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Normalize ``rect`` and clamp it onto the unrotated page; enforce a
    minimum side. Raises ValueError when it cannot form a valid area."""
    x0, x1 = sorted((rect[0], rect[2]))
    y0, y1 = sorted((rect[1], rect[3]))
    page_w, page_h = _page_bounds(page)
    x0, x1 = max(0.0, x0), min(page_w, x1)
    y0, y1 = max(0.0, y0), min(page_h, y1)
    if (x1 - x0) < _MIN_LINK_SIDE or (y1 - y0) < _MIN_LINK_SIDE:
        raise ValueError("that link area is too small")
    return (x0, y0, x1, y1)


def normalize_uri(text: str | None) -> str | None:
    """User input → a real URI target, or None when it can't be one.

    LOAD-BEARING: MuPDF decides a link's KIND by parsing the URI string, and a
    string with no scheme is stored as a file-LAUNCH action whose target is
    then lost (probe: "www.example.com" reads back kind=launch, uri=None). So
    every address is given a scheme before it is written — ``www.x.com`` and
    ``x.com`` become ``http://…``, a bare address becomes ``mailto:…`` — and
    anything that still isn't a plausible target is REFUSED rather than
    silently written as a broken launch action.
    """
    t = (text or "").strip()
    if not t:
        return None
    if "://" in t or t.lower().startswith(("mailto:", "tel:")):
        return t  # already carries a scheme
    if " " in t:
        return None  # nonsense — never a valid address
    if "@" in t:
        local, _, host = t.rpartition("@")
        return f"mailto:{t}" if local and "." in host else None
    if "." in t.strip("."):
        return "http://" + t  # bare host / host+path
    return None


def _prepare_uri(uri: str | None) -> str:
    """Normalize a URI for writing, raising when it can't be made valid."""
    normalized = normalize_uri(uri)
    if normalized is None:
        raise ValueError(
            f"{uri!r} is not a usable web or email address — try https://example.com "
            "or name@example.com"
        )
    return normalized


def _infer_kind(uri: str | None, dest_page: int | None) -> str:
    if uri is not None and dest_page is not None:
        raise ValueError("a link is either a URI or a go-to-page, not both")
    if uri is not None:
        if not uri.strip():
            raise ValueError("the link address is empty")
        return URI
    if dest_page is not None:
        return GOTO
    raise ValueError("a link needs a URI or a destination page")


def _write_dict(
    doc: pymupdf.Document,
    rect: tuple[float, float, float, float],
    *,
    kind: str,
    uri: str | None,
    dest_page: int | None,
    dest_point: tuple[float, float] | None,
    xref: int | None = None,
) -> dict:
    """Build an ``insert_link``/``update_link`` dict (``from`` UNROTATED)."""
    d: dict = {"kind": _KIND_TO_PYMUPDF[kind], "from": pymupdf.Rect(rect)}
    if xref is not None:
        d["xref"] = xref
    if kind == URI:
        # The ONE place a URI reaches the page — normalized here so no write
        # path can create a scheme-less (broken launch-action) link.
        d["uri"] = _prepare_uri(uri)
    else:  # GOTO
        if dest_page is None or not (0 <= dest_page < doc.page_count):
            raise ValueError("the destination page is out of range")
        d["page"] = int(dest_page)
        d["to"] = pymupdf.Point(dest_point) if dest_point is not None else pymupdf.Point(0, 0)
    return d


def add_link(
    doc: pymupdf.Document,
    page_index: int,
    rect: tuple[float, float, float, float],
    *,
    uri: str | None = None,
    dest_page: int | None = None,
    dest_point: tuple[float, float] | None = None,
) -> int:
    """Create a link over ``rect`` (unrotated page points). Returns its xref.

    Exactly one of ``uri`` (a URI link) or ``dest_page`` (a go-to-page link)
    must be given.
    """
    kind = _infer_kind(uri, dest_page)
    page = doc[page_index]
    rect = _clamp_rect(page, rect)
    d = _write_dict(doc, rect, kind=kind, uri=uri, dest_page=dest_page, dest_point=dest_point)
    before = {int(lk.get("xref", 0)) for lk in page.get_links()}
    page.insert_link(d)
    page = _refresh(doc, page_index, page)
    fresh = [lk for lk in page.get_links() if int(lk.get("xref", 0)) not in before]
    return int(fresh[0]["xref"]) if fresh else 0


def _find_raw(page: pymupdf.Page, xref: int) -> dict:
    for lk in page.get_links():
        if int(lk.get("xref", 0)) == xref:
            return lk
    raise ValueError(f"no link with xref {xref} on this page")


def _rewrite(
    doc: pymupdf.Document,
    page_index: int,
    xref: int,
    *,
    offset: tuple[float, float] | None = None,
    new_rect: tuple[float, float, float, float] | None = None,
    uri: str | None = None,
    dest_page: int | None = None,
    dest_point: tuple[float, float] | None = None,
    change_target: bool = False,
) -> None:
    """Update a link in place (its xref is preserved). Keeps whatever is not
    being changed; only editable (URI/GOTO) links can be rewritten.

    The caller MUST NOT hold its own reference to the page while calling this:
    ``reload_page`` refuses to evict a page with more than one live Python
    reference, so all geometry (``offset``) is resolved here, not by the caller.

    Works on ANY link kind. A geometry-only change keeps the link's existing
    target verbatim (its RAW dict, whatever the kind) — moving or resizing a
    hotspot never needs to understand where it points; a target change REPLACES
    the destination with a URI/go-to-page, which is equally valid for a link
    whose old target we could not represent (that is how a broken scheme-less
    link gets repaired).
    """
    page = doc[page_index]
    raw = _find_raw(page, xref)
    info = _link_info(page_index, page, raw)
    if offset is not None:
        x0, y0, x1, y1 = info.bbox
        rect = _clamp_rect(page, (x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]))
    else:
        rect = _clamp_rect(page, new_rect if new_rect is not None else info.bbox)
    if change_target:
        kind = _infer_kind(uri, dest_page)
        d = _write_dict(
            doc, rect, kind=kind, uri=uri, dest_page=dest_page, dest_point=dest_point, xref=xref
        )
    else:  # geometry only — preserve the raw target, whatever kind it is
        d = {k: v for k, v in raw.items() if k != "id"}
        d["from"] = pymupdf.Rect(rect)
        d["xref"] = xref
    page.update_link(d)
    _refresh(doc, page_index, page)


def update_link(
    doc: pymupdf.Document,
    page_index: int,
    xref: int,
    *,
    uri: str | None = None,
    dest_page: int | None = None,
    dest_point: tuple[float, float] | None = None,
) -> None:
    """Change WHERE a link points (its rect stays). Exactly one of ``uri`` or
    ``dest_page`` — a URI link may become a go-to-page link and vice versa."""
    _rewrite(
        doc,
        page_index,
        xref,
        uri=uri,
        dest_page=dest_page,
        dest_point=dest_point,
        change_target=True,
    )


def move_link(
    doc: pymupdf.Document, page_index: int, xref: int, offset: tuple[float, float]
) -> None:
    """Translate a link's rectangle by ``offset`` (unrotated page points)."""
    _rewrite(doc, page_index, xref, offset=offset)


def resize_link(
    doc: pymupdf.Document,
    page_index: int,
    xref: int,
    new_rect: tuple[float, float, float, float],
) -> None:
    """Resize a link's rectangle to ``new_rect`` (unrotated page points)."""
    _rewrite(doc, page_index, xref, new_rect=new_rect)


def delete_link(doc: pymupdf.Document, page_index: int, xref: int) -> None:
    """Remove the link with ``xref`` (any kind)."""
    page = doc[page_index]
    page.delete_link(_find_raw(page, xref))
    _refresh(doc, page_index, page)


def add_link_rects(
    doc: pymupdf.Document,
    page_index: int,
    rects: list[tuple[float, float, float, float]],
    *,
    uri: str | None = None,
    dest_page: int | None = None,
    dest_point: tuple[float, float] | None = None,
) -> list[int]:
    """Create one link per rect, all sharing the same target — a multi-line
    text link is one annotation per line (how real PDFs do it). Rects too small
    to form a valid area are skipped. Returns the new links' xrefs."""
    kind = _infer_kind(uri, dest_page)
    page = doc[page_index]
    before = {int(lk.get("xref", 0)) for lk in page.get_links()}
    for rect in rects:
        try:
            r = _clamp_rect(page, rect)
        except ValueError:
            continue
        page.insert_link(
            _write_dict(doc, r, kind=kind, uri=uri, dest_page=dest_page, dest_point=dest_point)
        )
    page = _refresh(doc, page_index, page)
    return [
        int(lk.get("xref", 0)) for lk in page.get_links() if int(lk.get("xref", 0)) not in before
    ]


def underline_rects(
    doc: pymupdf.Document,
    page_index: int,
    rects: list[tuple[float, float, float, float]],
    *,
    color: tuple[float, float, float] = WORD_LINK_BLUE_RGB,
    thickness: float | None = None,
) -> None:
    """Draw an ADDITIVE underline near the bottom of each rect (page content,
    non-destructive). The fallback "link style" for text that can't be
    recoloured in place (embedded/outline/scanned). Unrotated page points —
    the stroke rotates with the page like any content."""
    page = doc[page_index]
    for x0, y0, x1, y1 in rects:
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            continue
        t = thickness if thickness is not None else max(0.6, (y1 - y0) * 0.06)
        yb = y1 - max(1.0, (y1 - y0) * 0.12)  # just below the text baseline
        page.draw_line(pymupdf.Point(x0, yb), pymupdf.Point(x1, yb), color=color, width=t)


# --- redaction survival guard -------------------------------------------------


class _LinkGuard:
    """Keeps links alive across a redaction-based mutation.

    ``apply_redactions`` REMOVES any annotation whose rect intersects a
    redaction band — links included (probe-confirmed 1→0). Snapshot the page's
    links before the mutation, then :meth:`restore` re-inserts whatever was
    killed. ``moved`` = (old_bbox, dx, dy) declares a content region the wrapped
    op TRANSLATED (a paragraph/image move): links whose centre sits in it follow
    the move, whether or not the redaction killed them.
    """

    def __init__(
        self,
        doc: pymupdf.Document,
        page_index: int,
        moved: tuple[tuple[float, float, float, float], float, float] | None = None,
    ) -> None:
        self._doc = doc
        self._page_index = page_index
        self._moved = moved
        self._before = _capture_specs(doc[page_index])

    def restore(self) -> None:
        # apply_redactions reflects in get_links immediately (unlike the link
        # mutation APIs), so the alive-set is accurate without a reload. The
        # re-inserts below do NOT reload the page (the caller holds it, which
        # reload_page refuses); they still land in the document bytes, which is
        # what preserves the link across save/undo. The live get_links cache
        # for this page self-heals on the next reopen.
        page = self._doc[self._page_index]
        alive = {int(lk.get("xref", 0)) for lk in page.get_links()}
        for xref, (cx, cy), spec in self._before:
            in_move = False
            if self._moved is not None:
                (bx0, by0, bx1, by1), dx, dy = self._moved
                if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                    in_move = True
                    r = pymupdf.Rect(spec["from"]) + (dx, dy, dx, dy)
                    spec = {**spec, "from": r}
            if xref not in alive:
                page.insert_link(spec)
            elif in_move:  # survived but should follow the moved content
                page.update_link({**spec, "xref": xref})


def _capture_specs(page: pymupdf.Page) -> list[tuple[int, tuple[float, float], dict]]:
    """Re-insertable specs (``from`` UNROTATED, any kind) + each link's xref and
    rect centre, for the guard."""
    dm = page.derotation_matrix
    out: list[tuple[int, tuple[float, float], dict]] = []
    for lk in page.get_links():
        r = pymupdf.Rect(lk["from"]) * dm
        r.normalize()
        spec = {k: v for k, v in lk.items() if k not in ("id", "xref")}
        spec["from"] = r
        out.append((int(lk.get("xref", 0)), ((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2), spec))
    return out


def guard(
    doc: pymupdf.Document,
    page_index: int,
    moved: tuple[tuple[float, float, float, float], float, float] | None = None,
) -> _LinkGuard:
    """Snapshot links before a redaction-based op; call ``.restore()`` after."""
    return _LinkGuard(doc, page_index, moved)


# --- URL auto-detection -------------------------------------------------------

# A token IS a web/email address (matched whole, after trailing punctuation is
# trimmed). Kept deliberately simple — the detector links only confident hits.
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"  # http(s):// or www.
    r"|mailto:\S+"  # explicit mailto:
    r"|[\w.+-]+@[\w-]+\.[\w.-]+",  # bare email
    re.IGNORECASE,
)
# Punctuation commonly trailing a URL in prose, trimmed before matching/linking.
_TRAILING = ".,;:!?)]}>\"'"


@dataclass(frozen=True)
class DetectedUrl:
    """A URL/email found in page text: its normalized target and word rect."""

    uri: str
    rect: tuple[float, float, float, float]
    text: str


def detect_urls(doc: pymupdf.Document, page_index: int) -> list[DetectedUrl]:
    """Every URL/email in page ``n``'s text, as normalized targets + word rects
    (unrotated page points). One hit per word; trailing prose punctuation is
    trimmed from the target (the rect keeps the whole word — harmless)."""
    page = doc[page_index]
    out: list[DetectedUrl] = []
    seen: set[tuple] = set()
    for w in page.get_text("words"):
        x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
        core = word.strip().rstrip(_TRAILING)
        if not core or not _URL_RE.fullmatch(core):
            continue
        uri = normalize_uri(core)  # the same scheme rule every write path uses
        if uri is None:
            continue
        rect = (float(x0), float(y0), float(x1), float(y1))
        key = (round(x0, 1), round(y0, 1), core.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(DetectedUrl(uri, rect, word))
    return out


def _para_style_editable(para) -> bool:
    return bool(para.spans) and all(s.rotation == 0 and not s.embedded for s in para.spans)


def _rect_in_para(rect: tuple[float, float, float, float], para) -> bool:
    px0, py0, px1, py1 = para.bbox
    return px0 - 2 <= rect[0] and rect[2] <= px1 + 2 and py0 - 2 <= rect[1] and rect[3] <= py1 + 2


def link_detected_urls(
    doc: pymupdf.Document,
    page_index: int,
    *,
    style: bool = True,
    color: int = WORD_LINK_BLUE,
) -> int:
    """Find URLs/emails on page ``n`` and turn them into hyperlinks — recoloured
    + underlined where the text is editable, an additive underline otherwise.
    Returns how many were linked.

    Paragraphs are styled ONCE with all their URL rects (a second restyle would
    use a stale Paragraph); links are added AFTER all styling (which redacts).
    """
    from pdfcore import textedit

    detected = detect_urls(doc, page_index)
    if not detected:
        return 0
    if style:
        groups: dict[tuple, tuple] = {}  # paragraph identity -> (para, [rects])
        for du in detected:
            cx, cy = (du.rect[0] + du.rect[2]) / 2, (du.rect[1] + du.rect[3]) / 2
            para = textedit.paragraph_at(doc, page_index, cx, cy)
            if para is not None and _para_style_editable(para) and _rect_in_para(du.rect, para):
                entry = groups.setdefault((para.bbox, para.text), (para, []))
                entry[1].append(du.rect)
            else:  # not recolourable — draw the fallback underline now
                underline_rects(doc, page_index, [du.rect], color=WORD_LINK_BLUE_RGB)
        for para, rects in groups.values():
            textedit.style_paragraph_selection(
                doc, page_index, para, rects, color=color, underline=True
            )
    for du in detected:
        add_link_rects(doc, page_index, [du.rect], uri=du.uri)
    return len(detected)
