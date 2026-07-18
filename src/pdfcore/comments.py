"""Review comments: FreeText annotations that never flatten (E11). Pure PyMuPDF.

A comment is MARKUP, not content — the deliberate opposite of inserted text
boxes (which are real page content from birth). Comments render on screen,
appear in Acrobat's comment pane (replyable, author + dates shown), and do
NOT print by default anywhere: their PDF Print flag stays cleared, which
conforming viewers honour, and our own print path hides them unless the
print-dialog option is on.

Kinds (probe-verified native on 1.28.0):
- "note"    — a tinted FreeText box.
- "callout" — FreeText with a straight leader line + OPEN arrowhead to a
  target point (``IT=/FreeTextCallout``). The ``callout=`` kwarg needs TWO
  points — (target, attach-point-on-the-box) — a single point is silently
  ignored (no CL written, no leader: the E11.5 bug). ``border_width`` is
  ALSO load-bearing: it is the stroke width of the leader, arrowhead and
  box border alike, and the default 0 strokes them invisibly thin — so
  callouts pass ``_BORDER_W`` (3 pt, user choice), which is what makes the
  leader visible AND gives the callout box its border. Leader, arrowhead
  and border are RED (``_LEADER_COLOR``): MuPDF couples the stroke colour
  to the DA text colour (which must stay black), so the appearance patch
  recolours the stroke (see ``_pad_appearance``). The head is OPEN by
  design (E11.12): a closed head's fill is not semantically expressible —
  every regenerator fills it from the box tint, so an Acrobat edit lost
  the red fill — and an open head regenerates true.
Circle/Square annotation shapes are native too and can join later; ROUNDED
rectangles are not in the PDF annotation model (Square has no corner
radius) — they would need drawn-content emulation, deliberately out of
scope.

Geometry (E11.5): the box SHRINKWRAPS its text — sized from the same helv
metrics MuPDF lays out with (``get_text_length``; explicit newlines kept,
long lines word-wrapped at ``_MAX_TEXT_W``) plus ``_PAD`` padding all round.
MuPDF itself always glues FreeText text to the box's top-left (~0-1.5 pt,
no inset API; the richtext CSS path interprets padding/margin
unpredictably — probe-measured 4 pt asked, ~35 pt got), so after every
appearance regeneration ``_pad_appearance`` nudges the ONE text block in
the generated AP stream (``q 1 0 -0 1 tx ty cm BT``) down-right by the
padding. That is the single AP-stream touch in the codebase; if a future
PyMuPDF reformats the stream the regex no-ops and only the padding is
lost. Consequence: EVERY comment mutation must funnel through ``_create``
(edits/moves delete + recreate, preserving author/created/target — the
returned xref CHANGES), and nothing else may call ``annot.update()`` on a
comment afterwards; ``set_comments_hidden`` therefore flips flags WITHOUT
update() (probe-verified effective — rendering reads flags live).

For callouts pymupdf auto-expands Rect to enclose the leader and records
the text box via /RD, so ``CommentInfo.rect`` derives the TEXT BOX back
(RD order as MuPDF writes it: [left, bottom, right, top] in PDF y-up
terms) — hit-testing and the extraction exclusions must not cover the
whole leader region. ``CommentInfo.target`` round-trips the leader tip
from /CL (stored y-up; converted via the mediabox).

Identity: ``subject == "PDFEditorComment"`` marks ours; the annotation TITLE
carries the author (Acrobat shows Title as the comment's author), and
creation/modification dates ride the standard info keys.

Comment text must never leak into the editing/extraction surfaces —
``comment_rects`` feeds the exclusion filters (extract_spans, textsource).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

SUBJECT = "PDFEditorComment"

# Chrome: a soft yellow note tint, black 9pt helv text. Callout leader,
# open half-size arrowhead and box border stroke RED at 2 pt (user
# choices, 2026-07-18; weight 3 → 2 same day).
_FILL = (1.0, 0.98, 0.78)
_FONT_SIZE = 9.0
_PAD = 4.0  # interior padding between box edge and text, pt
_LINE_H = 1.2 * _FONT_SIZE  # MuPDF's own FreeText leading (10.8 pt at 9 pt)
_MAX_TEXT_W = 288.0  # wrap width before the box stops growing (4 in)
_MIN_TEXT_W = 24.0  # a one-character comment still gets a grabbable box
_WRAP_GRACE = 1.0  # so MuPDF (same metrics) never wraps a line before us
_BORDER_W = 2.0  # callout leader/arrow/border stroke width
_LEADER_COLOR = (1.0, 0.0, 0.0)
# MuPDF's own text inset inside a bordered box SCALES with the border
# width (probe: 1.5 at width 1, 4.5 at width 3 — i.e. 1.5 × width).
_BORDER_INSET = 1.5 * _BORDER_W

# The one text block in a generated FreeText appearance stream.
_CM_RE = re.compile(rb"q\n1 0 -0 1 ([0-9.+-]+) ([0-9.+-]+) cm\nBT")
# The one stroke-colour op in a generated FreeText appearance stream.
_RG_RE = re.compile(rb"(?m)^[0-9. ]+RG$")
# The open arrowhead: the one wing→tip→wing two-l stroke path (the leader
# itself is a single-l path, so this shape is unique in the stream).
_ARROW_RE = re.compile(
    rb"([0-9.+-]+) ([0-9.+-]+) m\n"
    rb"([0-9.+-]+) ([0-9.+-]+) l\n"
    rb"([0-9.+-]+) ([0-9.+-]+) l\nS"
)
# MuPDF sizes the head proportionally to the line width (~10× at 3 pt —
# huge); there is no API knob, so the patch scales the wing points toward
# the tip. 0.5 = half size (user choice, 2026-07-18).
_ARROW_SCALE = 0.5


def _halve_arrow(match: re.Match[bytes]) -> bytes:
    l1x, l1y, tip_x, tip_y, l2x, l2y = (float(g) for g in match.groups())

    def toward_tip(x: float, y: float) -> tuple[float, float]:
        return tip_x + (x - tip_x) * _ARROW_SCALE, tip_y + (y - tip_y) * _ARROW_SCALE

    h1x, h1y = toward_tip(l1x, l1y)
    h2x, h2y = toward_tip(l2x, l2y)
    return (f"{h1x:.4f} {h1y:.4f} m\n{tip_x:.4f} {tip_y:.4f} l\n{h2x:.4f} {h2y:.4f} l\nS").encode(
        "ascii"
    )


def _rgb_op(color: tuple[float, float, float]) -> bytes:
    return f"{color[0]:g} {color[1]:g} {color[2]:g}".encode("ascii")


def _inner_pad(bordered: bool) -> float:
    """Padding from the box EDGE: the border stroke straddles the rect
    boundary, so bordered boxes get half a stroke of extra room to keep
    the visual text-to-border gap at ``_PAD``."""
    return _PAD + (_BORDER_W / 2 if bordered else 0.0)


@dataclass(frozen=True)
class CommentInfo:
    """One comment: annotation xref + placement + metadata.

    ``rect`` is always the TEXT BOX (for callouts the annotation's own Rect
    is the larger leader-enclosing union); ``target`` is the leader tip in
    page points for callouts, None for notes.
    """

    xref: int
    page: int
    rect: tuple[float, float, float, float]
    text: str
    author: str
    created: str  # PDF date string (D:YYYYMMDD...), "" when absent
    modified: str
    kind: str  # "note" | "callout"
    target: tuple[float, float] | None = None


def _is_comment(annot: pymupdf.Annot) -> bool:
    return annot.type[1] == "FreeText" and annot.info.get("subject") == SUBJECT


def _kind_of(doc: pymupdf.Document, xref: int) -> str:
    kind, value = doc.xref_get_key(xref, "IT")
    return "callout" if (kind == "name" and "Callout" in value) else "note"


def _wrap_lines(text: str) -> list[str]:
    """The lines MuPDF will lay out: explicit newlines kept, long lines
    greedily word-wrapped at ``_MAX_TEXT_W`` (same helv metrics)."""
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        current = words[0]
        for word in words[1:]:
            candidate = current + " " + word
            if (
                pymupdf.get_text_length(candidate, fontname="helv", fontsize=_FONT_SIZE)
                <= _MAX_TEXT_W
            ):
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines or [""]


def _fit_rect(page: pymupdf.Page, x0: float, y0: float, text: str, bordered: bool) -> pymupdf.Rect:
    """Shrinkwrapped text box anchored at (x0, y0), clamped onto the page."""
    lines = _wrap_lines(text)
    widest = max(
        pymupdf.get_text_length(line, fontname="helv", fontsize=_FONT_SIZE) for line in lines
    )
    pad = _inner_pad(bordered)
    width = max(widest, _MIN_TEXT_W) + 2 * pad + _WRAP_GRACE
    height = len(lines) * _LINE_H + 2 * pad
    page_w, page_h = page.rect.width, page.rect.height
    x0 = min(max(0.0, x0), max(0.0, page_w - width))
    y0 = min(max(0.0, y0), max(0.0, page_h - height))
    return pymupdf.Rect(x0, y0, x0 + width, y0 + height)


def _attach_point(box: pymupdf.Rect, target: tuple[float, float]) -> pymupdf.Point:
    """Leader attach point: the box-edge midpoint nearest the target."""
    tx, ty = target
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    candidates = (
        pymupdf.Point(box.x0, cy),
        pymupdf.Point(box.x1, cy),
        pymupdf.Point(cx, box.y0),
        pymupdf.Point(cx, box.y1),
    )
    return min(candidates, key=lambda p: (p.x - tx) ** 2 + (p.y - ty) ** 2)


def _pad_appearance(doc: pymupdf.Document, xref: int, bordered: bool) -> None:
    """Finish the generated AP stream: padding, and the callout chrome.

    Padding: nudge the one text block down-right so the text sits
    ``_inner_pad`` from the box's top-left (MuPDF glues it there; there is
    no inset API). Callout chrome (user choice, 2026-07-18): the open
    arrowhead is halved (wing points pulled toward the tip — MuPDF sizes
    it ~10× the line width with no knob) and the stroke colour op is
    rewritten to ``_LEADER_COLOR`` — MuPDF couples it to the DA text
    colour, which must stay black in the stream."""
    kind, value = doc.xref_get_key(xref, "AP/N")
    if kind != "xref":
        return
    ap_xref = int(value.split()[0])
    stream = doc.xref_stream(ap_xref)
    match = _CM_RE.search(stream)
    if match is None:  # unexpected stream shape: keep the unpadded appearance
        return
    shift = _inner_pad(bordered) - (_BORDER_INSET if bordered else 0.0)
    tx = float(match.group(1)) + shift
    ty = float(match.group(2)) - shift
    patched = (
        stream[: match.start()]
        + f"q\n1 0 -0 1 {tx:.4f} {ty:.4f} cm\nBT".encode("ascii")
        + stream[match.end() :]
    )
    if bordered:
        # Halve the arrowhead (wing points pulled toward the tip) and turn
        # the one stroke-colour op red (leader + arrowhead + border).
        patched = _ARROW_RE.sub(_halve_arrow, patched, count=1)
        patched = _RG_RE.sub(_rgb_op(_LEADER_COLOR) + b" RG", patched, count=1)
    doc.update_stream(ap_xref, patched)


def _create(
    doc: pymupdf.Document,
    page_index: int,
    x0: float,
    y0: float,
    text: str,
    author: str,
    target: tuple[float, float] | None,
    created: str | None = None,
    modified: str | None = None,
) -> int:
    """THE comment creation path — every add/edit/move funnels through here
    so leader, border, shrinkwrap and padding are always rebuilt together."""
    page = doc[page_index]
    rect = _fit_rect(page, x0, y0, text, bordered=target is not None)
    kwargs: dict = {}
    if target is not None:
        kwargs["callout"] = (pymupdf.Point(*target), _attach_point(rect, target))
        # OPEN arrow (E11.12, user choice): a closed head's FILL is not
        # semantically expressible (regenerators fill it from the box tint,
        # so an Acrobat edit lost it) — the open head regenerates true.
        kwargs["line_end"] = pymupdf.PDF_ANNOT_LE_OPEN_ARROW
        kwargs["border_width"] = _BORDER_W
    annot = page.add_freetext_annot(
        rect,
        text,
        fontsize=_FONT_SIZE,
        fontname="helv",
        text_color=(0, 0, 0),
        fill_color=_FILL,
        **kwargs,
    )
    now = pymupdf.get_pdf_now()
    annot.set_info(
        title=author, subject=SUBJECT, creationDate=created or now, modDate=modified or now
    )
    annot.set_flags(0)  # no PRINT bit — markup, not page content
    annot.update()
    _pad_appearance(doc, annot.xref, bordered=target is not None)
    if target is not None:
        _write_regeneration_style(doc, annot.xref, text)
    return annot.xref


def _write_regeneration_style(doc: pymupdf.Document, xref: int, text: str) -> None:
    """Semantic styling so OTHER viewers regenerate the callout chrome.

    Our red 3 pt leader/border/arrowhead lives in the appearance stream we
    patch — authoritative for display everywhere, but Acrobat REBUILDS the
    appearance from the annotation dictionary the moment a user edits the
    comment there, and the dictionary said "black" (user screenshot: an
    Acrobat edit reverted the callout to black chrome). Encode the
    convention Acrobat itself uses for callouts: the /DA colour is the
    LINE + border colour (red — Acrobat renders the TEXT from /RC when
    present, so DA no longer speaks for the text), /RC + /DS carry the
    black 9 pt Helvetica text, and /C stays the box fill. The keys are
    written RAW after the appearance patch; nothing may ``annot.update()``
    afterwards (the standing rule), so OUR appearance is untouched. Not
    expressible semantically (an Acrobat edit reverts it): the head's
    HALF size — the open-arrow SHAPE itself regenerates true (E11.12).
    """
    import html

    r, g, b = _LEADER_COLOR
    doc.xref_set_key(
        xref, "DA", pymupdf.get_pdf_str(f"{r:g} {g:g} {b:g} rg /Helv {_FONT_SIZE:g} Tf")
    )
    paragraphs = "".join(f'<p dir="ltr">{html.escape(line)}</p>' for line in text.split("\n"))
    rich = (
        '<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" '
        'xfa:APIVersion="Acrobat:8.0.0" xfa:spec="2.4" '
        f'style="font-size:{_FONT_SIZE:g}pt;color:#000000;font-family:Helvetica">'
        f"{paragraphs}</body>"
    )
    doc.xref_set_key(xref, "RC", pymupdf.get_pdf_str(rich))
    doc.xref_set_key(
        xref,
        "DS",
        pymupdf.get_pdf_str(f"font: Helvetica {_FONT_SIZE:g}pt; text-align:left; color:#000000"),
    )


def add_comment(
    doc: pymupdf.Document,
    page_index: int,
    rect: tuple[float, float, float, float],
    text: str,
    author: str,
    callout_target: tuple[float, float] | None = None,
) -> int:
    """Create a comment; returns its annotation xref.

    ``rect`` anchors the box's top-left; the SIZE is computed from the text
    (shrinkwrap + padding), so the passed width/height are ignored.
    ``callout_target`` turns it into a callout — a straight leader line with
    an arrowhead from the box to that page point, and a box border in the
    leader's colour. The Print flag stays CLEARED (flags=0): the comment
    shows on screen everywhere but prints nowhere by default.
    """
    return _create(doc, page_index, rect[0], rect[1], text, author, callout_target)


def _text_box(doc: pymupdf.Document, annot: pymupdf.Annot) -> tuple[float, float, float, float]:
    """The comment's text box in page points. For callouts the annot Rect is
    the leader-enclosing union; /RD holds the differences back to the box
    (MuPDF writes [left, bottom, right, top] in PDF y-up terms)."""
    rect = annot.rect
    kind, value = doc.xref_get_key(annot.xref, "RD")
    if kind == "array":
        try:
            rd = [float(v) for v in value.strip("[] ").split()]
        except ValueError:
            rd = []
        if len(rd) == 4:
            # Rect (and RD) carry half a border stroke of slop on every side
            # so the stroke isn't clipped — shrink back to the true text box.
            # (Notes have border width 0 AND a zero RD, so they are no-ops.)
            width = annot.border.get("width") or 0.0
            half = width / 2 if width > 0 else 0.0
            inner = (
                rect.x0 + rd[0] + half,
                rect.y0 + rd[3] + half,
                rect.x1 - rd[2] - half,
                rect.y1 - rd[1] - half,
            )
            if inner[2] > inner[0] and inner[3] > inner[1]:
                return inner
    return (rect.x0, rect.y0, rect.x1, rect.y1)


def _target_of(doc: pymupdf.Document, page: pymupdf.Page, xref: int) -> tuple[float, float] | None:
    """The callout leader tip in page points (CL is stored y-up)."""
    kind, value = doc.xref_get_key(xref, "CL")
    if kind != "array":
        return None
    try:
        nums = [float(v) for v in value.strip("[] ").split()]
    except ValueError:
        return None
    if len(nums) < 4:
        return None
    box = page.mediabox
    return (nums[0] - box.x0, box.y1 - nums[1])


def comments_on_page(doc: pymupdf.Document, page_index: int) -> list[CommentInfo]:
    """Every comment on the page (page kept referenced — orphan gotcha)."""
    page = doc[page_index]
    result: list[CommentInfo] = []
    for annot in page.annots():
        if not _is_comment(annot):
            continue
        info = annot.info
        kind = _kind_of(doc, annot.xref)
        result.append(
            CommentInfo(
                xref=annot.xref,
                page=page_index,
                rect=_text_box(doc, annot),
                text=info.get("content", ""),
                author=info.get("title", ""),
                created=info.get("creationDate", ""),
                modified=info.get("modDate", ""),
                kind=kind,
                target=_target_of(doc, page, annot.xref) if kind == "callout" else None,
            )
        )
    return result


def comment_rects(
    doc: pymupdf.Document, page_index: int
) -> list[tuple[float, float, float, float]]:
    """Comment box rects — the exclusion regions for text extraction."""
    return [c.rect for c in comments_on_page(doc, page_index)]


def comment_at(doc: pymupdf.Document, page_index: int, px: float, py: float) -> CommentInfo | None:
    """The comment under a page point (smallest rect wins), or None."""
    best: CommentInfo | None = None
    best_area = float("inf")
    for comment in comments_on_page(doc, page_index):
        x0, y0, x1, y1 = comment.rect
        if x0 <= px <= x1 and y0 <= py <= y1:
            area = (x1 - x0) * (y1 - y0)
            if area < best_area:
                best, best_area = comment, area
    return best


def _find_annot(page: pymupdf.Page, xref: int) -> pymupdf.Annot:
    for annot in page.annots():
        if annot.xref == xref:
            return annot
    raise ValueError("comment not found (it may have been deleted)")


_KEEP = object()  # sentinel: _recreate keeps the current target


def _recreate(
    doc: pymupdf.Document,
    page_index: int,
    xref: int,
    *,
    anchor: tuple[float, float] | None = None,
    text: str | None = None,
    target=_KEEP,
) -> int:
    """Delete + re-add through ``_create`` (identity preserved: author,
    creation date, kind, target). Returns the NEW xref."""
    page = doc[page_index]
    annot = _find_annot(page, xref)
    info = annot.info
    box = _text_box(doc, annot)
    current = _target_of(doc, page, xref) if _kind_of(doc, xref) == "callout" else None
    new_target = current if target is _KEEP else target
    new_text = info.get("content", "") if text is None else text
    x0, y0 = (box[0], box[1]) if anchor is None else anchor
    author = info.get("title", "")
    created = info.get("creationDate", "") or None
    page.delete_annot(annot)
    return _create(doc, page_index, x0, y0, new_text, author, new_target, created=created)


def update_comment_text(doc: pymupdf.Document, page_index: int, xref: int, text: str) -> int:
    """Rewrite a comment's text; the box re-shrinkwraps around it in place
    (top-left anchored) and the modification date bumps. Returns the NEW
    xref — the annotation is recreated, so the old one is gone."""
    return _recreate(doc, page_index, xref, text=text)


def move_comment(
    doc: pymupdf.Document,
    page_index: int,
    xref: int,
    rect: tuple[float, float, float, float],
) -> int:
    """Re-anchor the text box at ``rect``'s top-left (size stays text-fitted;
    a callout keeps its target, the leader re-attaches to the moved box).
    Returns the NEW xref — the annotation is recreated."""
    return _recreate(doc, page_index, xref, anchor=(rect[0], rect[1]))


def move_comment_target(
    doc: pymupdf.Document,
    page_index: int,
    xref: int,
    target: tuple[float, float],
) -> int:
    """Re-point a callout's arrowhead at a new page point (box and text
    stay put; the leader re-attaches). Returns the NEW xref."""
    if _kind_of(doc, xref) != "callout":
        raise ValueError("only callouts have an arrowhead to move")
    return _recreate(doc, page_index, xref, target=(float(target[0]), float(target[1])))


class _CommentGuard:
    """Keeps comments alive across a redaction-based mutation.

    ``apply_redactions`` REMOVES annotations whose rect intersects a
    redaction band (probe-confirmed: moving a text box under a callout's
    leader-union rect deleted the whole callout). Snapshot before the
    mutation, then :meth:`restore` re-creates whatever was killed —
    author, dates, kind and target preserved. ``moved`` = (old_bbox, dx,
    dy) declares a content region the wrapped op TRANSLATED: callout
    targets inside it follow the move (user request: the arrowhead moves
    with the text box it points at), whether or not the redaction killed
    their annotation.
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
        self._before = comments_on_page(doc, page_index)

    def restore(self) -> None:
        alive = {c.xref for c in comments_on_page(self._doc, self._page_index)}
        for c in self._before:
            target = c.target
            if self._moved is not None and target is not None:
                (bx0, by0, bx1, by1), dx, dy = self._moved
                if bx0 <= target[0] <= bx1 and by0 <= target[1] <= by1:
                    target = (target[0] + dx, target[1] + dy)
            if c.xref not in alive:
                _create(
                    self._doc,
                    self._page_index,
                    c.rect[0],
                    c.rect[1],
                    c.text,
                    c.author,
                    target,
                    created=c.created or None,
                    modified=(c.modified or None) if target == c.target else None,
                )
            elif target != c.target:
                _recreate(self._doc, self._page_index, c.xref, target=target)


def guard(
    doc: pymupdf.Document,
    page_index: int,
    moved: tuple[tuple[float, float, float, float], float, float] | None = None,
) -> _CommentGuard:
    """Snapshot comments before a redaction-based op; call ``.restore()``
    after it (see :class:`_CommentGuard`)."""
    return _CommentGuard(doc, page_index, moved)


def delete_comment(doc: pymupdf.Document, page_index: int, xref: int) -> None:
    page = doc[page_index]
    annot = _find_annot(page, xref)
    page.delete_annot(annot)


def set_comments_hidden(doc: pymupdf.Document, hidden: bool) -> int:
    """Hide/show every comment in the document; returns how many changed.

    The print path wraps its rendering in hide→render→show when "print
    comments" is off: pixmap rendering draws visible annotations regardless
    of their Print flag, so the flag alone does not keep comments off OUR
    paper (it does in other viewers). Flags only — deliberately NO
    ``annot.update()``: rendering reads flags live (probe-verified), and an
    update would regenerate the appearance and throw away the padding nudge.
    """
    changed = 0
    for page in doc:
        for annot in page.annots():
            if not _is_comment(annot):
                continue
            flags = annot.flags
            if hidden:
                new_flags = flags | pymupdf.PDF_ANNOT_IS_HIDDEN
            else:
                new_flags = flags & ~pymupdf.PDF_ANNOT_IS_HIDDEN
            if new_flags != flags:
                annot.set_flags(new_flags)
                changed += 1
    return changed
