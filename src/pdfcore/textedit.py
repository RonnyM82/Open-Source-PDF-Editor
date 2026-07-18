"""Text-span extraction and base-14 font mapping (Phase 2).

Pure PyMuPDF; no Qt. All coordinates are UNROTATED page points (top-left
origin, y down): ``get_text``, ``add_redact_annot`` and ``insert_text`` all
speak this space, so page rotation never crosses the engine boundary — the UI's
coordinate seam (pdfapp/page_coords.py) owns rotation.

Font rule (see CLAUDE.md): spans whose font is a standard non-embedded family
map to a PyMuPDF base-14 code so replacement text can be reinserted
NON-EMBEDDED — every viewer then substitutes old and new text identically.
Embedded fonts are never reproduced; they are flagged (``embedded=True``,
``base14=None``) for the UI's best-effort branch.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# get_text("dict") span flag bits. Verified empirically (flags-probe test and
# the real quote sample: Helvetica-Bold spans report flags=16).
FLAG_SUPERSCRIPT = 1
FLAG_ITALIC = 2
FLAG_SERIFED = 4
FLAG_MONOSPACED = 8
FLAG_BOLD = 16

# Subset-tagged fonts look like "ABCDEF+RealName". The tag only ever appears on
# embedded subset fonts — but only in get_page_fonts basefont entries; span
# font names from get_text("dict") come back ALREADY STRIPPED in 1.28.0, so
# per-span stripping is defensive only and the embedded lookup must resolve
# same-name collisions conservatively (see _embedded_font_map).
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")

# family -> (regular, bold, italic, bold-italic) base-14 codes.
_BASE14_CODES = {
    "helv": ("helv", "hebo", "heit", "hebi"),
    "tiro": ("tiro", "tibo", "tiit", "tibi"),
    "cour": ("cour", "cobo", "coit", "cobi"),
}


@dataclass(frozen=True)
class TextSpan:
    """One extracted text span, in unrotated page coordinates.

    - ``bbox``: ``(x0, y0, x1, y1)`` in PDF points.
    - ``origin``: baseline origin of the first glyph (the ``insert_text`` point).
    - ``font``: reported font name with any subset prefix stripped.
    - ``base14``: mapped base-14 code (``"helv"``/``"hebo"``/...), or ``None``
      when the font is embedded or has no standard-family match.
    - ``color``: sRGB int (0xRRGGBB) exactly as reported by ``get_text``.
    """

    page_index: int
    text: str
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]
    font: str
    base14: str | None
    size: float
    color: int
    flags: int
    embedded: bool
    # Quarter-turn text rotation from the line's ``dir`` (CAD dimension
    # labels run bottom-up = 90). None = an arbitrary angle we refuse to
    # edit (redact band + reinsertion only support quarter turns).
    rotation: int | None = 0


# Super/subscript have no font variants in PDF: rendered as a size scale plus
# a baseline shift, the way PDF producers themselves do it.
SCRIPT_NORMAL = 0
SCRIPT_SUPER = 1
SCRIPT_SUB = 2
_SCRIPT_SCALE = 0.58
_SUPER_RISE = 0.35  # of the base size, upward
_SUB_DROP = 0.15  # of the base size, downward


@dataclass(frozen=True)
class TextStyle:
    """An EXPLICIT style for inserted or replacement text (the style toolbar).

    ``fontfile`` set means a deliberate system-font choice — embedded as a
    subset (unlike automatic matching of existing text, which never embeds;
    an explicit choice can only be honoured by embedding). ``code`` is the
    base-14 fallback used when ``fontfile`` is None. ``underline`` draws a
    vector line under each inserted line (PDF has no underline fonts).
    """

    code: str = "helv"
    fontfile: str | None = None
    size: float = 11.0
    color: int = 0x000000
    underline: bool = False
    script: int = SCRIPT_NORMAL


def _effective_size(style: TextStyle) -> float:
    return style.size * _SCRIPT_SCALE if style.script != SCRIPT_NORMAL else style.size


def _baseline_shift(style: TextStyle) -> float:
    if style.script == SCRIPT_SUPER:
        return -style.size * _SUPER_RISE
    if style.script == SCRIPT_SUB:
        return style.size * _SUB_DROP
    return 0.0


def _font_kwargs(style: TextStyle) -> dict:
    """insert_text/insert_textbox font arguments for a style.

    The alias embeds a hash of the file PATH: same-stem files from different
    directories must not collide (a colliding alias silently reuses the
    first-registered font on the page — review finding).
    """
    if style.fontfile is None:
        return {"fontname": style.code}
    stem = re.sub(r"[^A-Za-z0-9]", "", Path(style.fontfile).stem) or "font"
    digest = hashlib.md5(str(Path(style.fontfile).resolve()).encode()).hexdigest()[:6]
    return {"fontname": f"sty{stem[:12]}{digest}", "fontfile": style.fontfile}


def _style_label(style: TextStyle) -> str:
    return Path(style.fontfile).stem if style.fontfile else style.code


def _style_text_width(style: TextStyle, text: str) -> float:
    size = _effective_size(style)
    if style.fontfile is None:
        return pymupdf.get_text_length(text, fontname=style.code, fontsize=size)
    return pymupdf.Font(fontfile=style.fontfile).text_length(text, fontsize=size)


@dataclass(frozen=True)
class StyledRun:
    """A stretch of text in ONE style (E9 — selection-level styling).

    Rich text = a sequence of runs; ``\\n`` inside a run's text is a hard
    line break. The engine lays runs out itself (word wrap, per-run baseline
    shift/underline) — widths measured with the same font resolution the
    inserts use, and baselines placed explicitly, so none of the
    ``insert_textbox`` page-state metric problems apply.
    """

    text: str
    style: TextStyle


@dataclass(frozen=True)
class _Fragment:
    """One laid-out piece of a visual line: text at an x-offset."""

    text: str
    style: TextStyle
    x: float
    width: float


_TOKEN_RE = re.compile(r"\S+|[^\S\n]+")


def _layout_runs(runs: list[StyledRun], wrap_width: float | None) -> list[list[_Fragment]]:
    """Break runs into visual lines of fragments (greedy word wrap).

    ``wrap_width`` None = hard ``\\n`` breaks only. A single word wider than
    the wrap width is placed anyway (overflows honestly — never force-broken
    mid-word). Trailing spaces are dropped from each visual line; adjacent
    same-style fragments are merged (continuous underlines, fewer inserts).
    """
    # Logical lines: sequences of (token, is_space, style) split on hard \n.
    logical: list[list[tuple[str, bool, TextStyle]]] = [[]]
    for run in runs:
        text = run.text.replace("\r\n", "\n").replace("\r", "\n")
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                logical.append([])
            for match in _TOKEN_RE.finditer(part):
                token = match.group(0)
                logical[-1].append((token, token.isspace(), run.style))

    lines: list[list[_Fragment]] = []
    for tokens in logical:
        current: list[_Fragment] = []
        x = 0.0
        for token, is_space, style in tokens:
            width = _style_text_width(style, token)
            if wrap_width is not None and not is_space and current and x + width > wrap_width:
                lines.append(_finish_line(current))
                current, x = [], 0.0
            if is_space and not current:
                continue  # drop leading spaces on a wrapped line
            current.append(_Fragment(token, style, x, width))
            x += width
        lines.append(_finish_line(current))
    return lines


def _finish_line(fragments: list[_Fragment]) -> list[_Fragment]:
    """Drop trailing spaces, then merge adjacent same-style fragments."""
    while fragments and fragments[-1].text.isspace():
        fragments.pop()
    merged: list[_Fragment] = []
    for frag in fragments:
        if merged and merged[-1].style == frag.style:
            last = merged[-1]
            merged[-1] = _Fragment(
                last.text + frag.text, last.style, last.x, last.width + frag.width
            )
        else:
            merged.append(frag)
    return merged


# Unit vectors per quarter-turn rotation (page space, y down): the direction
# the baseline advances, and the direction descenders extend (+shift moves
# text that way; underlines sit that side of the baseline).
_BASELINE_DIR = {0: (1.0, 0.0), 90: (0.0, -1.0), 180: (-1.0, 0.0), 270: (0.0, 1.0)}
_DESCENDER_DIR = {0: (0.0, 1.0), 90: (1.0, 0.0), 180: (0.0, -1.0), 270: (-1.0, 0.0)}


def _insert_line(
    page: pymupdf.Page,
    origin_x: float,
    baseline_y: float,
    fragments: list[_Fragment],
    rotation: int = 0,
) -> None:
    bx, by = _BASELINE_DIR[rotation]
    dx, dy = _DESCENDER_DIR[rotation]
    for frag in fragments:
        if not frag.text:
            continue
        shift = _baseline_shift(frag.style)
        rc = page.insert_text(
            (origin_x + frag.x * bx + shift * dx, baseline_y + frag.x * by + shift * dy),
            frag.text,
            fontsize=_effective_size(frag.style),
            color=srgb_to_rgb(frag.style.color),
            rotate=rotation,
            **_font_kwargs(frag.style),
        )
        if rc < 1:
            raise ValueError(f"could not place text fragment {frag.text!r}")
        if frag.style.underline and frag.text.strip():
            off = shift + max(0.6, _effective_size(frag.style) * 0.08)
            start = frag.x
            end = frag.x + frag.width
            _draw_underline(
                page,
                (origin_x + start * bx + off * dx, baseline_y + start * by + off * dy),
                (origin_x + end * bx + off * dx, baseline_y + end * by + off * dy),
                _effective_size(frag.style),
                frag.style.color,
            )


def _runs_have_text(runs: list[StyledRun]) -> bool:
    return any(run.text.strip() for run in runs)


def _dominant_label(runs: list[StyledRun]) -> str:
    if not runs:
        return "helv"
    best = max(runs, key=lambda r: len(r.text))
    return _style_label(best.style)


@dataclass(frozen=True)
class Paragraph:
    """A user-editable multi-line unit: a uniform-pitch run of lines.

    NOT a MuPDF dict block — those over-group (a table header can share a
    block with the body under it). A paragraph is the maximal run of lines
    around a seed line, within ONE dict block, whose baseline pitch matches
    the block's dominant pitch. ``spans`` carries every member span (for the
    per-span redaction bands); style fields are the dominant style by text
    length. ``pitch`` is the baseline distance between lines (fallback
    ``1.2 × size`` for single-line paragraphs).
    """

    page_index: int
    text: str  # lines joined with "\n"
    bbox: tuple[float, float, float, float]
    first_origin: tuple[float, float]
    pitch: float
    spans: tuple[TextSpan, ...]
    font: str
    base14: str | None
    size: float
    color: int
    flags: int
    embedded: bool
    uniform_style: bool
    # Member spans grouped by visual line (E9): lets callers rebuild rich
    # runs that PRESERVE per-word styling through edits and moves.
    lines: tuple[tuple[TextSpan, ...], ...] = ()
    # Detected justification from the original line geometry (E9.6):
    # "left" | "right" | "center". Reproduced on re-insert; "left" when
    # ambiguous (single line, or edges too irregular to call).
    align: str = "left"


# Line edges within this many points of each other count as "aligned".
_ALIGN_TOL = 1.5


def _detect_alignment(line_tuples: tuple[tuple[TextSpan, ...], ...]) -> str:
    """Infer a paragraph's justification from its lines' x-extents.

    Right-aligned blocks (a quote's totals labels/values) have equal RIGHT
    edges and ragged left ones; centred blocks share midpoints. Checked in
    left → right → center order so a block whose lines happen to be equal
    width stays "left" (indistinguishable, and left is the do-no-harm
    default). Single-line paragraphs are ambiguous → "left".
    """
    if len(line_tuples) < 2:
        return "left"
    x0s = [min(s.bbox[0] for s in line) for line in line_tuples]
    x1s = [max(s.bbox[2] for s in line) for line in line_tuples]
    if max(x0s) - min(x0s) <= _ALIGN_TOL:
        return "left"
    if max(x1s) - min(x1s) <= _ALIGN_TOL:
        return "right"
    mids = [(a + b) / 2 for a, b in zip(x0s, x1s, strict=True)]
    if max(mids) - min(mids) <= _ALIGN_TOL:
        return "center"
    return "left"


@dataclass(frozen=True)
class ParagraphReplaceResult:
    """Outcome of :func:`replace_paragraph_text` (fit is pre-checked: text
    that cannot fit even after growth raises BEFORE any mutation).

    ``resized``: the box had to grow downward to fit the new text (the UI
    mentions it — growth may overlay whatever sits below the paragraph).
    """

    inserted: bool
    used_font: str
    exact_font: bool
    uniform_style: bool
    resized: bool = False
    # The re-laid-out box extent (unrotated page pts): x spans the wrap box,
    # y the first line's ascent to the last line's descent. None when nothing
    # was inserted (empty replacement). Lets the UI keep an inserted box's
    # registry rect in step with moves AND grow/shrink edits (E10).
    new_bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ReplaceResult:
    """Outcome of :func:`replace_span_text`.

    - ``inserted``: False when ``new_text`` was empty (pure delete).
    - ``overflow``: new text is wider than the old bbox. Nothing moves, shrinks
      or wraps (no reflow — CLAUDE.md out-of-scope); the text overruns to the
      right and the UI warns.
    - ``used_font``: the base-14 code actually inserted with.
    - ``exact_font``: False ⇒ the UI shows "font can't be matched exactly".
    """

    inserted: bool
    overflow: bool
    used_font: str
    exact_font: bool


# MuPDF removes every character whose box INTERSECTS the redact rect, and
# char boxes span the line's full ascender..descender height — so the redact
# rect must be TIGHTER than the span bbox in both axes:
#
# - Horizontally: a small inset spares abutting same-line neighbours.
# - Vertically: a thin BAND around the baseline, NOT the bbox. Real documents
#   set lines tighter than their font metrics — in the sample quote the
#   description cell's line bboxes overlap by 2.39 pt, so a bbox-based rect
#   eats the line above (found in the manual pass). Any band strictly inside
#   the target's line box still intersects every target character.
_REDACT_INSET = 0.25  # horizontal, pt
_BAND_ABOVE_BASELINE = 0.60  # band top: origin_y - 0.60 x fontsize
_BAND_CLEAR_BASELINE = 0.10  # band bottom: origin_y - 0.10 x fontsize


def strip_subset_prefix(font_name: str) -> str:
    """Drop a leading ``ABCDEF+`` subset tag (exactly six uppercase letters)."""
    return _SUBSET_PREFIX.sub("", font_name)


def map_font_to_base14(font_name: str, flags: int = 0) -> str | None:
    """Map a reported font name (+ span flags) to a base-14 code, or ``None``.

    Name substrings are the primary bold/italic signal; span flag bits
    (bold=16, italic=2) are OR'd in as a fallback for fonts whose style is not
    in the name.
    """
    name = strip_subset_prefix(font_name).lower()
    bold = "bold" in name or bool(flags & FLAG_BOLD)
    italic = "italic" in name or "oblique" in name or bool(flags & FLAG_ITALIC)
    if "helvetica" in name or "arial" in name:
        family = "helv"
    elif "times" in name:
        family = "tiro"
    elif "courier" in name:
        family = "cour"
    elif "symbol" in name:
        return "symb"
    elif "zapf" in name or "dingbat" in name:
        return "zadb"
    else:
        return None
    regular, bold_code, italic_code, bold_italic = _BASE14_CODES[family]
    if bold and italic:
        return bold_italic
    if bold:
        return bold_code
    if italic:
        return italic_code
    return regular


def srgb_to_rgb(color: int) -> tuple[float, float, float]:
    """Convert a span's sRGB int (0xRRGGBB) to ``(r, g, b)`` floats in 0..1."""
    return ((color >> 16 & 255) / 255.0, (color >> 8 & 255) / 255.0, (color & 255) / 255.0)


# line "dir" -> quarter-turn rotation (probe-verified against insert_text's
# rotate= on 1.28.0: rotate=90 produces dir (0,-1) and origins round-trip).
def _rotation_from_dir(direction: tuple[float, float]) -> int | None:
    dx, dy = direction
    for rotation, (ex, ey) in ((0, (1, 0)), (90, (0, -1)), (180, (-1, 0)), (270, (0, 1))):
        if abs(dx - ex) <= 1e-3 and abs(dy - ey) <= 1e-3:
            return rotation
    return None  # arbitrary angle — extraction keeps it, editing refuses


def _line_rotation(line: dict) -> int | None:
    return _rotation_from_dir(line.get("dir", (1.0, 0.0)))


def _span_from_raw(
    page_index: int,
    raw: dict,
    embedded_by_font: dict[str, bool],
    rotation: int | None = 0,
) -> TextSpan:
    font = strip_subset_prefix(raw["font"])
    # Unmatched names default to embedded=True: the conservative branch (flag
    # to the user) rather than a wrong exact-match.
    embedded = embedded_by_font.get(font, True)
    base14 = None if embedded else map_font_to_base14(raw["font"], raw["flags"])
    return TextSpan(
        page_index=page_index,
        text=raw["text"],
        bbox=tuple(raw["bbox"]),
        origin=tuple(raw["origin"]),
        font=font,
        base14=base14,
        size=float(raw["size"]),
        color=int(raw["color"]),
        flags=int(raw["flags"]),
        embedded=embedded,
        rotation=rotation,
    )


def extract_spans(doc: pymupdf.Document, page_index: int) -> list[TextSpan]:
    """All text spans of one page, with mapped fonts and embedded detection.

    Review-comment annotation text leaks into ``get_text("dict")`` (E11
    probe) and must NEVER enter the editing surface — a comment is markup,
    not editable page content — so spans whose centre falls inside a comment
    rect are dropped here, at the single extraction gate.
    """
    from pdfcore import comments as comments_module

    page = doc[page_index]
    embedded_by_font = _embedded_font_map(doc, page_index)
    comment_rects = comments_module.comment_rects(doc, page_index)

    def outside_comments(raw: dict) -> bool:
        if not comment_rects:
            return True
        bx = raw["bbox"]
        cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
        return not any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in comment_rects)

    return [
        _span_from_raw(page_index, raw, embedded_by_font, _line_rotation(line))
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", ())
        for raw in line["spans"]
        if outside_comments(raw)
    ]


def replace_span_text(
    doc: pymupdf.Document,
    page_index: int,
    span: TextSpan,
    new_text: str,
    *,
    fill: tuple[float, float, float] | bool = False,
    style: TextStyle | None = None,
) -> ReplaceResult:
    """Replace one span's text: tight redact, then reinsert non-embedded.

    The redact rect is a thin baseline band inside the span bbox (see the
    band constants above — a full-bbox rect eats vertically overlapping
    neighbour lines) and redactions are applied with images and line-art
    PRESERVED (``IMAGE_NONE`` / ``LINE_ART_NONE`` — the 1.28 defaults would
    eat covered gridlines and blank overlapped image parts). ``fill``
    defaults to ``False`` (NO fill): removing the text reveals the page
    background, so a white quote stays white, while a fill rect would paint
    over any table border or shaded cell UNDER the band (a moved box whose
    band crossed a gridline was cutting it — E9.10). Reinsertion uses the
    span's mapped base-14 code at the original baseline origin, size and
    colour — ``helv`` as best effort when unmapped/embedded.

    Destructive at the PDF level (apply_redactions cannot be reversed); undo
    restores document STATE via snapshots, it never un-redacts. ``span`` is a
    value snapshot — after ANY mutation of the page, re-extract before editing
    again (a stale bbox redacts whatever sits there now).

    Single-line only: the UI edits one span in place, so ``new_text`` must not
    contain newlines (insert_text would start laying out extra lines below).
    """
    if "\n" in new_text or "\r" in new_text:
        raise ValueError("replacement text must be a single line")
    if style is None:  # automatic matching of the original (never embeds)
        style = TextStyle(code=span.base14 or "helv", size=span.size, color=span.color)
        exact_font = span.base14 is not None and not span.embedded
    else:  # explicit user style — honoured as chosen
        exact_font = True
    runs = [StyledRun(new_text, style)] if new_text else []
    return replace_span_runs(doc, page_index, span, runs, fill=fill, exact_font=exact_font)


def replace_span_runs(
    doc: pymupdf.Document,
    page_index: int,
    span: TextSpan,
    runs: list[StyledRun],
    *,
    fill: tuple[float, float, float] | bool = False,
    exact_font: bool = True,
) -> ReplaceResult:
    """Replace one span with RICH runs on a single line (E9).

    Same redaction as :func:`replace_span_text`; the runs are laid out left
    to right from the span's baseline origin, each with its own style
    (baseline-shifted scripts, per-run underline). Single-line: no run may
    contain a newline. ``exact_font`` is reporting-only.
    """
    for run in runs:
        if "\n" in run.text or "\r" in run.text:
            raise ValueError("replacement text must be a single line")
    rotation = span.rotation
    if rotation is None:  # BEFORE any mutation — the band would be wrong
        raise ValueError("this text is rotated at an unsupported angle")
    from pdfcore import comments as comments_module

    page = doc[page_index]
    band = _redact_band(span)
    foreign = _capture_foreign_spans(doc, page_index, [band], [span.bbox])
    comment_guard = comments_module.guard(doc, page_index)
    page.add_redact_annot(band, fill=fill)
    _apply_text_only_redactions(page)
    _repair_foreign_spans(doc, page_index, foreign, [span.bbox])
    _remove_member_underlines(page, [span])
    comment_guard.restore()

    lines = _layout_runs(runs, None)
    fragments = lines[0] if lines else []
    inserted = False
    overflow = False
    if any(frag.text.strip() for frag in fragments):
        _insert_line(page, span.origin[0], span.origin[1], fragments, rotation=rotation)
        inserted = True
        total_width = fragments[-1].x + fragments[-1].width
        # The old run's extent along ITS baseline axis (bbox height for
        # vertical text).
        span_extent = (
            (span.bbox[2] - span.bbox[0]) if rotation in (0, 180) else (span.bbox[3] - span.bbox[1])
        )
        overflow = total_width > span_extent + 0.5
    return ReplaceResult(
        inserted=inserted,
        overflow=overflow,
        used_font=_dominant_label(runs),
        exact_font=exact_font,
    )


def _band_interval(edge0: float, edge1: float, near: float, far: float) -> tuple[float, float]:
    """Clamp the [near, far] band to the bbox edges, mid-bbox fallback."""
    lo, hi = max(edge0, min(near, far)), min(edge1, max(near, far))
    if hi - lo < 0.5:  # degenerate metrics — mid-bbox fallback
        centre = (edge0 + edge1) / 2
        lo, hi = centre - 0.25, centre + 0.25
    return lo, hi


def _redact_band(span: TextSpan) -> pymupdf.Rect:
    """The tight redact rect for one span (see the band constants above).

    The band runs 0.10–0.60 × size from the baseline toward the ASCENDERS —
    for rotated text that axis turns with the glyphs (90° bottom-up text has
    its ascenders toward -x, per the CAD sample and the rotate-insert probe),
    while the full bbox extent (inset 0.25pt) follows the baseline axis.
    """
    x0, y0, x1, y1 = span.bbox
    ox, oy = span.origin
    near = _BAND_CLEAR_BASELINE * span.size
    far = _BAND_ABOVE_BASELINE * span.size
    rotation = span.rotation or 0
    if rotation in (90, 270):
        inset_y = min(_REDACT_INSET, (y1 - y0) / 4)
        sign = -1.0 if rotation == 90 else 1.0  # ascender axis: 90 -> -x, 270 -> +x
        left, right = _band_interval(x0, x1, ox + sign * near, ox + sign * far)
        return pymupdf.Rect(left, y0 + inset_y, right, y1 - inset_y)
    inset_x = min(_REDACT_INSET, (x1 - x0) / 4)
    sign = -1.0 if rotation == 0 else 1.0  # ascender axis: 0 -> -y (up), 180 -> +y
    top, bottom = _band_interval(y0, y1, oy + sign * near, oy + sign * far)
    return pymupdf.Rect(x0 + inset_x, top, x1 - inset_x, bottom)


def _draw_underline(
    page: pymupdf.Page,
    start: tuple[float, float],
    end: tuple[float, float],
    size: float,
    color: int,
) -> None:
    """Draw an underline between two PRE-COMPUTED points (the caller places
    them along the run's baseline axis — rotation-safe)."""
    page.draw_line(
        pymupdf.Point(*start),
        pymupdf.Point(*end),
        color=srgb_to_rgb(color),
        width=max(0.4, size * 0.05),
    )


def _apply_text_only_redactions(page: pymupdf.Page) -> None:
    """Apply pending redactions removing ONLY text — images/line-art preserved."""
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )


# The zone below a baseline where _insert_line places underlines (offset
# max(0.6, 0.08 × size) along the descender axis), with slack either side.
_UNDERLINE_ZONE_NEAR = 0.3
_UNDERLINE_ZONE_SLACK = 1.5


def _remove_member_underlines(page: pymupdf.Page, spans: Sequence[TextSpan]) -> None:
    """Remove underline strokes previously drawn under the given spans.

    Underlines are vector line-art WE draw (``_draw_underline``), and the
    text redaction deliberately PRESERVES line art (table borders) — so
    editing or deleting underlined text left its lines behind (user
    report, 2026-07-18: an emptied text box kept its underlines). A
    candidate stroke is a single-segment line path parallel to a member's
    baseline, sitting in that member's underline zone, and NO WIDER than
    the member (+1 pt): a table gridline runs wider, so it is never a
    candidate — and removal uses ``LINE_ART_REMOVE_IF_COVERED`` with a
    rect hugging each stroke, so nothing longer than the rect can die.
    Text and images are untouched (``TEXT_NONE`` / ``IMAGE_NONE``);
    re-insertion happens AFTER this pass, so fresh underlines survive.
    """
    candidates: list[pymupdf.Rect] = []
    for path in page.get_drawings():
        items = path.get("items", ())
        if len(items) != 1 or items[0][0] != "l":
            continue
        p1, p2 = items[0][1], items[0][2]
        for span in spans:
            rotation = span.rotation if span.rotation in _BASELINE_DIR else 0
            bx, by = _BASELINE_DIR[rotation]
            dx, dy = _DESCENDER_DIR[rotation]
            base_d = span.origin[0] * dx + span.origin[1] * dy
            d1 = p1.x * dx + p1.y * dy - base_d
            d2 = p2.x * dx + p2.y * dy - base_d
            if abs(d1 - d2) > 0.3:  # not parallel to the baseline
                continue
            far = max(0.6, span.size * 0.08) + _UNDERLINE_ZONE_SLACK
            if not (_UNDERLINE_ZONE_NEAR <= (d1 + d2) / 2 <= far):
                continue
            corners = ((span.bbox[0], span.bbox[1]), (span.bbox[2], span.bbox[3]))
            b_lo = min(cx * bx + cy * by for cx, cy in corners) - 1.0
            b_hi = max(cx * bx + cy * by for cx, cy in corners) + 1.0
            b1 = p1.x * bx + p1.y * by
            b2 = p2.x * bx + p2.y * by
            if min(b1, b2) < b_lo or max(b1, b2) > b_hi:
                continue  # wider than the member: a gridline, not our underline
            margin = 0.3 + (path.get("width") or 1.0) / 2
            candidates.append(
                pymupdf.Rect(
                    min(p1.x, p2.x) - margin,
                    min(p1.y, p2.y) - margin,
                    max(p1.x, p2.x) + margin,
                    max(p1.y, p2.y) + margin,
                )
            )
            break
    if not candidates:
        return
    for rect in candidates:
        page.add_redact_annot(rect)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
        text=pymupdf.PDF_REDACT_TEXT_NONE,
    )


@dataclass(frozen=True)
class _ProtectedGlyph:
    """A foreign glyph captured before a redaction so it can be re-drawn.

    ``rotation`` is the parent line's quarter-turn (0/90/180/270; None =
    arbitrary angle) — the restore must pass it to ``insert_text`` or a
    rotated bystander's glyphs come back drawn horizontally (probe-caught).
    """

    char: str
    origin: tuple[float, float]
    code: str
    size: float
    color: int
    rotation: int | None = 0


def _bbox_matches(a: tuple, b: tuple, tol: float = 0.6) -> bool:
    return all(abs(u - v) <= tol for u, v in zip(a, b, strict=True))


@dataclass(frozen=True)
class _ForeignSpan:
    """A whole bystander span whose glyphs a redaction band touches.

    Captured span-wise (E10.3): re-drawing individual missing glyphs kept the
    page VISUALLY exact but fragmented the span into glyph confetti in the
    extraction — the next edit of that text then grouped and re-flowed
    fragments ("the existing textbox gets broken up"). Repair therefore
    rebuilds affected spans WHOLE (one insert_text each) whenever their font
    maps to base-14 (identical metrics → identical glyph positions).
    """

    text: str
    origin: tuple[float, float]
    bbox: tuple[float, float, float, float]
    code: str | None
    size: float
    color: int
    rotation: int | None
    embedded: bool
    glyphs: tuple[_ProtectedGlyph, ...]  # non-space glyphs, for presence checks


def _covered_by(inner: tuple, outer: tuple, tol: float = 1.0) -> bool:
    return (
        inner[0] >= outer[0] - tol
        and inner[1] >= outer[1] - tol
        and inner[2] <= outer[2] + tol
        and inner[3] <= outer[3] + tol
    )


def _capture_foreign_spans(
    doc: pymupdf.Document,
    page_index: int,
    bands: list[pymupdf.Rect],
    member_bboxes: list[tuple[float, float, float, float]],
    covered_bboxes: list[tuple[float, float, float, float]] = (),
) -> list[_ForeignSpan]:
    """Whole spans that are NOT part of the edited span(s) but have at least
    one glyph inside a redaction band — the innocent bystanders
    ``apply_redactions`` would clip (it removes EVERY glyph intersecting the
    rect, not just ours).

    Captured BEFORE the redaction so ``_repair_foreign_spans`` can put them
    back afterwards. Membership is matched at SPAN level by bbox (rawdict and
    dict share the same extraction geometry), so overlapping foreign text is
    still told apart from ours.
    """
    page = doc[page_index]
    embedded_by_font = _embedded_font_map(doc, page_index)
    captured: list[_ForeignSpan] = []
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            rotation = _line_rotation(line)
            for span in line.get("spans", []):
                if any(_bbox_matches(span["bbox"], mb) for mb in member_bboxes):
                    continue  # our own text — re-inserted as the edit itself
                if any(_covered_by(span["bbox"], cb) for cb in covered_bboxes):
                    # A remnant of a span being REBUILT whole: pass-1 clipping
                    # re-extracts it with a smaller bbox, so equality matching
                    # misses it — protecting it would resurrect the remnant
                    # under the whole re-insert (found in the repair probe).
                    continue
                chars = span.get("chars", [])
                hit = False
                glyphs: list[_ProtectedGlyph] = []
                font = strip_subset_prefix(span["font"])
                code = map_font_to_base14(font, span.get("flags", 0))
                for ch in chars:
                    if not ch["c"].strip():
                        continue
                    cb = pymupdf.Rect(ch["bbox"])
                    glyphs.append(
                        _ProtectedGlyph(
                            char=ch["c"],
                            origin=(ch["origin"][0], ch["origin"][1]),
                            code=code or "helv",
                            size=span["size"],
                            color=span["color"],
                            rotation=rotation,
                        )
                    )
                    if not cb.is_empty and any(cb.intersects(b) for b in bands):
                        hit = True
                if hit and glyphs:
                    captured.append(
                        _ForeignSpan(
                            text="".join(ch["c"] for ch in chars),
                            origin=(chars[0]["origin"][0], chars[0]["origin"][1]),
                            bbox=tuple(span["bbox"]),
                            code=code,
                            size=span["size"],
                            color=span["color"],
                            rotation=rotation,
                            embedded=embedded_by_font.get(font, False),
                            glyphs=tuple(glyphs),
                        )
                    )
    return captured


def _surviving_glyph_keys(page: pymupdf.Page) -> set[tuple[str, float, float]]:
    """(char, origin) keys of every glyph currently on the page (rawdict).

    Survivors re-extract with bit-identical origins (their content ops were
    untouched), so 0.1-pt rounding gives stable keys.
    """
    keys: set[tuple[str, float, float]] = set()
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    keys.add((ch["c"], round(ch["origin"][0], 1), round(ch["origin"][1], 1)))
    return keys


def _glyph_key(g: _ProtectedGlyph) -> tuple[str, float, float]:
    return (g.char, round(g.origin[0], 1), round(g.origin[1], 1))


def _span_stub(fs: _ForeignSpan) -> TextSpan:
    """A TextSpan carrying just the geometry ``_redact_band`` reads."""
    return TextSpan(
        page_index=0,
        text=fs.text,
        bbox=fs.bbox,
        origin=fs.origin,
        font="",
        base14=fs.code,
        size=fs.size,
        color=fs.color,
        flags=0,
        embedded=fs.embedded,
        rotation=fs.rotation,
    )


def _whole_span_reinsertable(fs: _ForeignSpan) -> bool:
    """Whole re-insertion reproduces the span EXACTLY for QUARTER-TURN
    base-14-mapped non-embedded text — standard metrics give identical glyph
    positions, and ``insert_text(rotate=)`` round-trips origin and direction
    (probe-verified, same primitive as rotated editing). Arbitrary angles
    (rotation None) and embedded/unmapped fonts fall back to per-glyph."""
    return fs.rotation in (0, 90, 180, 270) and fs.code is not None and not fs.embedded


def _repair_foreign_spans(
    doc: pymupdf.Document,
    page_index: int,
    foreign: list[_ForeignSpan],
    exclude_bboxes: list[tuple[float, float, float, float]],
) -> None:
    """Put captured bystander spans back after ``apply_redactions`` (E10.3).

    MuPDF's removal predicate is stricter than bbox intersection, so a
    captured span can come out untouched, partially clipped, or fully gone —
    each is repaired differently:

    - untouched → nothing (re-drawing would double glyphs — the "EXISSTING"
      bug).
    - fully gone → re-insert the WHOLE span as one text run.
    - partially clipped → remove the surviving remnant with the span's own
      thin baseline band (the proven neighbour-sparing primitive) and
      re-insert the whole span — glyph confetti from per-glyph restore
      fragmented the span in extraction and broke later edits of that text.
      The removal pass protects ITS bystanders per-glyph (no recursion).
    - rotated/embedded/unmapped fonts → per-glyph restore of the missing
      glyphs only (whole re-insertion could not reproduce them exactly).
    """
    if not foreign:
        return
    page = doc[page_index]
    survivors = _surviving_glyph_keys(page)
    per_glyph: list[_ProtectedGlyph] = []
    reinsert_whole: list[_ForeignSpan] = []
    rebuild: list[_ForeignSpan] = []
    for fs in foreign:
        missing = [g for g in fs.glyphs if _glyph_key(g) not in survivors]
        if not missing:
            continue  # the redaction spared it entirely
        if not _whole_span_reinsertable(fs):
            per_glyph.extend(missing)
        elif len(missing) == len(fs.glyphs):
            reinsert_whole.append(fs)
        else:
            rebuild.append(fs)

    if rebuild:
        bands = [_redact_band(_span_stub(fs)) for fs in rebuild]
        collateral = _capture_foreign_spans(
            doc,
            page_index,
            bands,
            list(exclude_bboxes),
            covered_bboxes=[fs.bbox for fs in rebuild],
        )
        for band in bands:
            page.add_redact_annot(band, fill=False)
        _apply_text_only_redactions(page)
        survivors_after = _surviving_glyph_keys(page)
        for fs in collateral:  # thin bands rarely clip anything; restore if so
            per_glyph.extend(g for g in fs.glyphs if _glyph_key(g) not in survivors_after)

    for fs in reinsert_whole + rebuild:
        try:
            rc = page.insert_text(
                fs.origin,
                fs.text,
                fontname=fs.code or "helv",
                fontsize=fs.size,
                color=srgb_to_rgb(fs.color),
                rotate=fs.rotation or 0,
            )
            if rc < 1:
                raise ValueError("nothing inserted")
        except Exception:
            per_glyph.extend(fs.glyphs)  # degrade to glyph restore, lose nothing

    seen: set[tuple[str, float, float]] = set()
    for g in per_glyph:
        key = _glyph_key(g)
        if key in seen:
            continue
        seen.add(key)
        page.insert_text(
            g.origin,
            g.char,
            fontname=g.code,
            fontsize=g.size,
            color=srgb_to_rgb(g.color),
            rotate=g.rotation if g.rotation in (90, 180, 270) else 0,
        )


# Lines whose baseline delta differs from the block's dominant pitch by more
# than this are NOT part of the same paragraph (splits table headers from the
# body lines MuPDF groups into one dict block).
_PITCH_TOL = 0.7


def _block_units(lines: list[dict]) -> list[list[int]]:
    """Split a block's line INDICES into paragraph-candidate units.

    A unit is a maximal run of consecutive HORIZONTAL lines (pitch grouping
    applies within it); every rotated/arbitrary-angle line stands alone —
    its baseline advance is not comparable to horizontal neighbours', and a
    CAD dimension label must never read as a line of the paragraph next to
    it.
    """
    units: list[list[int]] = []
    horizontal: list[int] = []
    for i, line in enumerate(lines):
        if _line_rotation(line) == 0:
            horizontal.append(i)
            continue
        if horizontal:
            units.append(horizontal)
            horizontal = []
        units.append([i])
    if horizontal:
        units.append(horizontal)
    return units


def paragraph_at(
    doc: pymupdf.Document,
    page_index: int,
    px: float,
    py: float,
    pad: float = 1.0,
    boundaries: Sequence[tuple[float, float, float, float]] = (),
) -> Paragraph | None:
    """The paragraph under an (unrotated) page point, or None.

    Seeded by the line containing the point, grown up/down within its dict
    block while the baseline pitch matches the block's dominant (median)
    pitch. Dict blocks over-group (headers + body); separate blocks (e.g.
    different table rows) are never merged; rotated lines stand alone.
    ``boundaries`` (insert-box bboxes) additionally keep a line inside an
    isolation region from joining lines outside it.
    """
    page = doc[page_index]
    embedded_by_font = _embedded_font_map(doc, page_index)
    keep = _comment_line_filter(doc, page_index)
    # Hit-test in ORIGINAL block/line order — overlap ties (a box's line bbox
    # bleeding into a neighbour's) must resolve exactly as they always did.
    for block in page.get_text("dict")["blocks"]:
        lines = [line for line in block.get("lines", ()) if line["spans"] and keep(line)]
        hit = None
        for line in lines:
            bx0, by0, bx1, by1 = line["bbox"]
            if bx0 - pad <= px <= bx1 + pad and by0 - pad <= py <= by1 + pad:
                hit = line
                break
        if hit is None:
            continue
        if _line_owned(hit, boundaries):
            # A registered-box region IS one paragraph — page-wide, even when
            # MuPDF put its lines in different blocks (E10.7).
            region_lines, _plain = _partition_lines(page, boundaries, keep)
            region = _line_region(hit, boundaries)
            return _build_paragraph(page_index, region_lines[region], embedded_by_font)
        kept = [line for line in lines if not _line_owned(line, boundaries)]
        seed = kept.index(hit)
        unit = next(u for u in _block_units(kept) if seed in u)
        unit_lines = [kept[i] for i in unit]
        run = _pitch_run(unit_lines, unit.index(seed))
        return _build_paragraph(page_index, [kept[unit[j]] for j in run], embedded_by_font)
    return None


def _line_owned(line: dict, boundaries: Sequence[tuple[float, float, float, float]]) -> bool:
    """True when a line belongs to a registered-box region (the ONE ownership
    predicate — paragraph_at and _partition_lines must never disagree)."""
    return bool(boundaries) and _line_region(line, boundaries) >= 0 and _line_rotation(line) == 0


def _comment_line_filter(doc: pymupdf.Document, page_index: int):
    """Line predicate dropping review-comment annotation text.

    Comment text leaks into ``get_text("dict")`` like any FreeText — the
    span extraction gate (:func:`extract_spans`) already drops it, and the
    paragraph builders must apply the SAME exclusion or a comment reads as
    an editable paragraph (found by the E11.5 shrinkwrap tests)."""
    from pdfcore import comments as comments_module

    rects = comments_module.comment_rects(doc, page_index)
    if not rects:
        return lambda line: True

    def keep(line: dict) -> bool:
        bx = line["bbox"]
        cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
        return not any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rects)

    return keep


def _partition_lines(
    page: pymupdf.Page,
    boundaries: Sequence[tuple[float, float, float, float]],
    keep=lambda line: True,
) -> tuple[dict[int, list[dict]], list[list[dict]]]:
    """Split the page's dict lines into region-owned and plain groups.

    Lines whose centre falls inside a boundary rect (a registered insert box)
    belong to that REGION — one paragraph each, ordered by baseline, however
    MuPDF chose to block them. Everything else stays grouped per original
    block for the geometric pitch flow. Rotated lines never join a region
    (boxes are inserted horizontal; a rotated stray keeps its own rules).
    """
    region_lines: dict[int, list[dict]] = {}
    plain_blocks: list[list[dict]] = []
    for block in page.get_text("dict")["blocks"]:
        lines = [line for line in block.get("lines", ()) if line["spans"] and keep(line)]
        if not boundaries:
            if lines:
                plain_blocks.append(lines)
            continue
        kept: list[dict] = []
        for line in lines:
            if _line_owned(line, boundaries):
                region_lines.setdefault(_line_region(line, boundaries), []).append(line)
            else:
                kept.append(line)
        if kept:
            plain_blocks.append(kept)
    for lines in region_lines.values():
        lines.sort(key=_line_baseline)
    return region_lines, plain_blocks


def paragraphs_on_page(
    doc: pymupdf.Document,
    page_index: int,
    boundaries: Sequence[tuple[float, float, float, float]] = (),
) -> list[Paragraph]:
    """Every paragraph on the page, in block/line order (U1).

    The same partition :func:`paragraph_at` samples pointwise: each dict
    block's lines split into units (consecutive horizontal runs; rotated
    lines singleton), and each unit into maximal uniform-pitch runs — so a
    point inside any returned paragraph hands ``paragraph_at`` an equal
    paragraph. (``_pitch_run`` grows to the same delta boundaries from any
    seed inside a run, which is what makes successive-seed partitioning
    consistent.) ``boundaries`` isolate inserted boxes identically both ways.
    """
    page = doc[page_index]
    embedded_by_font = _embedded_font_map(doc, page_index)
    result: list[Paragraph] = []
    region_lines, plain_blocks = _partition_lines(
        page, boundaries, _comment_line_filter(doc, page_index)
    )
    for region in sorted(region_lines):
        result.append(_build_paragraph(page_index, region_lines[region], embedded_by_font))
    for lines in plain_blocks:
        for unit in _block_units(lines):
            unit_lines = [lines[i] for i in unit]
            seed = 0
            while seed < len(unit_lines):
                run = _pitch_run(unit_lines, seed)
                result.append(
                    _build_paragraph(page_index, [unit_lines[i] for i in run], embedded_by_font)
                )
                seed = run[-1] + 1
    return result


def _line_baseline(line: dict) -> float:
    return line["spans"][0]["origin"][1]


# A joinable line advance must be at least this (pt). CAD exporters place a
# section label at EACH end of the section line as separate dict "lines" on
# ONE baseline (delta ~0) — those are horizontal fragments, never stacked
# paragraph lines, so near-zero (and negative/out-of-order) deltas always
# break a run.
_MIN_LINE_PITCH = 0.7


def _line_region(line: dict, boundaries: Sequence[tuple[float, float, float, float]]) -> int:
    """Index of the isolation region (insert box) a line belongs to, or -1.

    A line whose bbox centre falls inside a boundary bbox is that region's;
    lines in DIFFERENT regions — or a region vs none — never join one
    paragraph (see ``_pitch_run``). The UI supplies boundaries for text it
    inserted this session, so a moved pre-existing paragraph never swallows
    an inserted box that happens to sit one line away.
    """
    bx0, by0, bx1, by1 = line["bbox"]
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    for i, (x0, y0, x1, y1) in enumerate(boundaries):
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return i
    return -1


def _pitch_run(lines: list[dict], seed: int, regions: list[int] | None = None) -> list[int]:
    """Indices of the maximal uniform-pitch run of lines around ``seed``.

    Only REAL line advances (``>= _MIN_LINE_PITCH``) can join a run, and the
    reference pitch is the median of the real advances only — a block of
    mostly same-baseline fragments (a CAD title block's cell labels) must
    not drag the reference to zero and glue everything together. ``regions``
    (per-line isolation ids) additionally forbids joining lines across an
    isolation boundary — an inserted box never merges with its neighbours.
    """
    if len(lines) == 1:
        return [0]
    baselines = [_line_baseline(line) for line in lines]
    deltas = [baselines[i + 1] - baselines[i] for i in range(len(baselines) - 1)]
    real = sorted(d for d in deltas if d >= _MIN_LINE_PITCH)
    if not real:
        return [seed]  # no vertical rhythm in this block — lines stand alone
    ref = real[len(real) // 2]  # the block's dominant pitch

    def joins(i: int) -> bool:  # advance between line i and i+1
        if regions is not None and regions[i] != regions[i + 1]:
            return False
        delta = deltas[i]
        return delta >= _MIN_LINE_PITCH and abs(delta - ref) <= _PITCH_TOL

    lo = hi = seed
    while lo > 0 and joins(lo - 1):
        lo -= 1
    while hi < len(lines) - 1 and joins(hi):
        hi += 1
    return list(range(lo, hi + 1))


def _build_paragraph(
    page_index: int, lines: list[dict], embedded_by_font: dict[str, bool]
) -> Paragraph:
    line_tuples = tuple(
        tuple(
            _span_from_raw(page_index, raw, embedded_by_font, _line_rotation(line))
            for raw in line["spans"]
        )
        for line in lines
    )
    spans = tuple(span for line in line_tuples for span in line)
    text = "\n".join("".join(raw["text"] for raw in line["spans"]) for line in lines)
    bbox = (
        min(s.bbox[0] for s in spans),
        min(s.bbox[1] for s in spans),
        max(s.bbox[2] for s in spans),
        max(s.bbox[3] for s in spans),
    )
    baselines = [_line_baseline(line) for line in lines]
    first_x = min(raw["origin"][0] for raw in lines[0]["spans"])
    first_origin = (first_x, baselines[0])

    # Dominant style by text length; uniform means every span matches it.
    def style_key(span: TextSpan) -> tuple:
        return (span.font, round(span.size, 1), span.color, span.flags)

    weights: dict[tuple, int] = {}
    for span in spans:
        weights[style_key(span)] = weights.get(style_key(span), 0) + len(span.text)
    dominant_key = max(weights, key=weights.get)  # type: ignore[arg-type]
    rep = next(s for s in spans if style_key(s) == dominant_key)
    uniform = all(style_key(s) == dominant_key for s in spans)

    if len(baselines) > 1:
        pitch = (baselines[-1] - baselines[0]) / (len(baselines) - 1)
    else:
        pitch = rep.size * 1.2
    return Paragraph(
        align=_detect_alignment(line_tuples),
        page_index=page_index,
        text=text,
        bbox=bbox,
        first_origin=first_origin,
        pitch=pitch,
        spans=spans,
        font=rep.font,
        base14=rep.base14,
        size=rep.size,
        color=rep.color,
        flags=rep.flags,
        embedded=rep.embedded,
        uniform_style=uniform,
        lines=line_tuples,
    )


def merge_paragraphs(paras: Sequence[Paragraph]) -> Paragraph:
    """One editable unit from several fragments (E10.7, user request).

    Repaired bystander lines land in separate MuPDF blocks, so a paragraph a
    box was moved across can fragment into several editable zones. Merging
    builds the UNION paragraph — commit it through ``replace_paragraph_runs``
    (same text, offset 0) and the lines are re-inserted as one contiguous
    run, so extraction sees ONE block again (a physical repair, not display
    trickery).

    Horizontal text on one page only. Lines are ordered by baseline;
    same-baseline lines from different fragments are stitched into one line
    (x order). Pitch = median inter-line advance — fragments of one original
    paragraph keep their exact baselines; merging UNRELATED boxes with
    different spacing re-pitches them uniformly (lines move — that is what
    "merge into one paragraph" means).
    """
    if len(paras) < 2:
        raise ValueError("select at least two text boxes to merge")
    if len({p.page_index for p in paras}) != 1:
        raise ValueError("text boxes on different pages can't be merged")
    for para in paras:
        for span in para.spans:
            if span.rotation != 0:
                raise ValueError("only horizontal text boxes can be merged")

    raw_lines = sorted(
        (line for para in paras for line in para.lines if line),
        key=lambda ln: (min(s.origin[1] for s in ln), min(s.bbox[0] for s in ln)),
    )
    grouped: list[list[TextSpan]] = []
    for line in raw_lines:
        baseline = min(s.origin[1] for s in line)
        if grouped and abs(min(s.origin[1] for s in grouped[-1]) - baseline) <= 0.5:
            grouped[-1] = sorted([*grouped[-1], *line], key=lambda s: s.bbox[0])
        else:
            grouped.append(sorted(line, key=lambda s: s.bbox[0]))
    line_tuples = tuple(tuple(line) for line in grouped)
    spans = tuple(span for line in line_tuples for span in line)

    text = "\n".join("".join(s.text for s in line) for line in line_tuples)
    bbox = (
        min(s.bbox[0] for s in spans),
        min(s.bbox[1] for s in spans),
        max(s.bbox[2] for s in spans),
        max(s.bbox[3] for s in spans),
    )
    baselines = [min(s.origin[1] for s in line) for line in line_tuples]
    first_origin = (min(s.origin[0] for s in line_tuples[0]), baselines[0])

    def style_key(span: TextSpan) -> tuple:
        return (span.font, round(span.size, 1), span.color, span.flags)

    weights: dict[tuple, int] = {}
    for span in spans:
        weights[style_key(span)] = weights.get(style_key(span), 0) + len(span.text)
    dominant_key = max(weights, key=weights.get)  # type: ignore[arg-type]
    rep = next(s for s in spans if style_key(s) == dominant_key)
    uniform = all(style_key(s) == dominant_key for s in spans)

    deltas = sorted(
        d
        for d in (baselines[i + 1] - baselines[i] for i in range(len(baselines) - 1))
        if d >= _MIN_LINE_PITCH
    )
    pitch = deltas[len(deltas) // 2] if deltas else rep.size * 1.2

    return Paragraph(
        align=_detect_alignment(line_tuples),
        page_index=paras[0].page_index,
        text=text,
        bbox=bbox,
        first_origin=first_origin,
        pitch=pitch,
        spans=spans,
        font=rep.font,
        base14=rep.base14,
        size=rep.size,
        color=rep.color,
        flags=rep.flags,
        embedded=rep.embedded,
        uniform_style=uniform,
        lines=line_tuples,
    )


def replace_paragraph_text(
    doc: pymupdf.Document,
    page_index: int,
    para: Paragraph,
    new_text: str,
    *,
    fill: tuple[float, float, float] | bool = False,
    offset: tuple[float, float] = (0.0, 0.0),
    style: TextStyle | None = None,
    width: float | None = None,
) -> ParagraphReplaceResult:
    """Replace a whole paragraph: band-redact every member span, then
    re-insert the new text wrapped WITHIN the paragraph's own box.

    Line pitch is reproduced from the original and the first baseline is
    pinned to the original first line's. ``\\n`` in ``new_text`` is a hard
    break; long lines wrap to the paragraph width. The box GROWS to fit:
    first a ~2% width bump (absorbs substituted-font metric drift — an
    original full-width line can re-measure a hair wider and would otherwise
    spuriously wrap, which is what broke same-text moves), then downward
    line-by-line for genuinely longer text (``resized=True`` in the result so
    the UI can mention it). Growth into OCCUPIED space is refused (E9.4) —
    see ``replace_paragraph_runs``. Text that cannot fit even after maximal
    growth raises ValueError, BEFORE any mutation. Mixed-style paragraphs
    are replaced in the DOMINANT style — ``uniform_style=False`` in the
    result tells the UI to warn (a MOVE of a mixed paragraph flattens it
    too).

    ``offset`` translates the re-insertion box (unrotated page points) —
    passing the paragraph's own text with an offset MOVES it. The translated
    box is clamped to stay on the page.
    """
    new_text = new_text.replace("\r\n", "\n").replace("\r", "\n")
    if style is None:  # automatic matching of the paragraph's dominant style
        style = TextStyle(code=para.base14 or "helv", size=para.size, color=para.color)
        exact_font = para.base14 is not None and not para.embedded
        pitch = para.pitch
    else:  # explicit user style; pitch scales with a size change
        exact_font = True
        pitch = para.pitch * (style.size / para.size) if para.size > 0 else para.pitch
    runs = [StyledRun(new_text, style)] if new_text else []
    return replace_paragraph_runs(
        doc,
        page_index,
        para,
        runs,
        fill=fill,
        offset=offset,
        width=width,
        pitch=pitch,
        exact_font=exact_font,
    )


# Wrap-width grace: ~2% (min 2pt). An originally full-width line re-measured
# with OUR font resolution can come out a hair wider than the recorded box —
# without the grace it spuriously wraps (this broke same-text moves).
_GROW_WIDTH_FACTOR = 0.02


def replace_paragraph_runs(
    doc: pymupdf.Document,
    page_index: int,
    para: Paragraph,
    runs: list[StyledRun],
    *,
    fill: tuple[float, float, float] | bool = False,
    offset: tuple[float, float] = (0.0, 0.0),
    width: float | None = None,
    pitch: float | None = None,
    exact_font: bool = True,
) -> ParagraphReplaceResult:
    """Replace a paragraph with RICH runs, laid out by the engine (E9).

    Selection-level styling: each run carries its own TextStyle (bold words,
    coloured/underlined stretches, per-character super/subscript). The engine
    word-wraps to the paragraph width (or ``width``), pins the first baseline
    to the original's (+``offset``), keeps the original pitch, and draws
    underlines from the computed layout. Because every baseline and x-offset
    is placed explicitly with ``insert_text``, none of the ``insert_textbox``
    page-state metric problems apply (the old calibration probe is gone).

    The text grows DOWNWARD when it needs more lines than the original box
    (``resized=True``); at the page bottom it slides up; text that cannot fit
    the page raises BEFORE any mutation. Growth is allowed only into BLANK
    space: an in-place edit (``offset == 0``) whose extra lines would land on
    text OUTSIDE the paragraph is refused pre-mutation (E9.4 — on the real
    quote the next table row sits exactly one pitch below, so a line break
    added mid-paragraph printed the spilled line straight over it). Deliberate
    MOVES (``offset != 0``) keep their place-anywhere semantics — the user is
    watching the rubber band. ``exact_font`` is reporting-only (the caller
    knows whether its font choices were honoured).

    Rotated text is refused here (BEFORE any mutation): rotated lines are
    singleton paragraphs by construction, and the UI edits them through the
    single-span path, which re-inserts with the rotation preserved.
    """
    if any(span.rotation != 0 for span in para.spans):
        raise ValueError("rotated text is edited as a single line, not a paragraph")
    pitch_val = pitch if pitch is not None else para.pitch
    wrap = max(20.0, width if width is not None else (para.bbox[2] - para.bbox[0]))
    lines = _layout_runs(runs, wrap * (1.0 + _GROW_WIDTH_FACTOR) + 2.0)
    while lines and not lines[-1]:
        lines.pop()  # trailing empty lines add height but render nothing
    has_text = any(frag.text.strip() for line in lines for frag in line)

    page = doc[page_index]
    page_w, page_h = page.rect.width, page.rect.height
    if page.rotation % 180 == 90:  # unrotated bounds (the space we insert in)
        page_w, page_h = page_h, page_w

    resized = False
    if has_text:
        max_size = max(
            (frag.style.size for line in lines for frag in line if frag.text.strip()),
            default=11.0,
        )
        ascent, descent = 1.1 * max_size, 0.35 * max_size
        needed = ascent + (len(lines) - 1) * pitch_val + descent
        if needed > page_h + 0.5:  # pre-flight: nothing mutated yet
            raise ValueError("The replacement text does not fit the paragraph box — shorten it.")
        origin_x = min(max(para.bbox[0] + offset[0], 0.0), max(0.0, page_w - wrap))
        first_baseline = para.first_origin[1] + offset[1]

        def line_shift(line: list[_Fragment]) -> float:
            """Justification x-shift (E9.6): right/centre-aligned paragraphs
            keep their right edge / midpoint; a line wider than the box (the
            wrap grace) overflows in the alignment's natural direction."""
            if para.align == "left" or not line:
                return 0.0
            slack = wrap - (line[-1].x + line[-1].width)
            return slack if para.align == "right" else slack / 2.0

        # Clamp onto the page: ascender room at the top, slide up at the bottom.
        lowest_first = page_h - descent - (len(lines) - 1) * pitch_val
        first_baseline = max(ascent, min(first_baseline, lowest_first))
        original_bottom = para.bbox[3] + offset[1] + 1.0
        resized = (first_baseline + (len(lines) - 1) * pitch_val + descent) > original_bottom + 0.25

        # Growth collision pre-flight (E9.4). New lines may claim baselines
        # OUTSIDE the original strip (below it, or above when slid up at the
        # page bottom). Blank space is fine; landing on text that is NOT part
        # of this paragraph would print straight over it, so that is refused
        # before any mutation. Only for in-place edits — a MOVE (offset != 0)
        # is deliberate placement.
        if offset == (0.0, 0.0):
            orig_first = para.first_origin[1]
            orig_last = max(s.origin[1] for s in para.spans)
            tol = 0.35 * pitch_val
            outside = [
                (first_baseline + i * pitch_val, line)
                for i, line in enumerate(lines)
                if line
                and not (orig_first - tol <= first_baseline + i * pitch_val <= orig_last + tol)
            ]
            if outside:
                members = set(para.spans)
                for other in extract_spans(doc, page_index):
                    if other in members or not other.text.strip():
                        continue
                    o_top = other.origin[1] - 0.8 * other.size
                    o_bot = other.origin[1] + 0.25 * other.size
                    for b, line in outside:
                        size = max(f.style.size for f in line)
                        x0 = origin_x + line_shift(line) + line[0].x
                        x1 = origin_x + line_shift(line) + line[-1].x + line[-1].width
                        if (
                            b - 0.8 * size < o_bot
                            and b + 0.25 * size > o_top
                            and x0 < other.bbox[2]
                            and x1 > other.bbox[0]
                        ):
                            raise ValueError(
                                "The edited text needs more lines than the paragraph "
                                "box has, and the space it would grow into is already "
                                "occupied by other text — shorten the text or remove "
                                "a line break."
                            )

    from pdfcore import comments as comments_module

    bands = [_redact_band(span) for span in para.spans]
    member_bboxes = [span.bbox for span in para.spans]
    foreign = _capture_foreign_spans(doc, page_index, bands, member_bboxes)
    comment_guard = comments_module.guard(
        doc,
        page_index,
        moved=(para.bbox, offset[0], offset[1]) if offset != (0.0, 0.0) else None,
    )
    for band in bands:
        page.add_redact_annot(band, fill=fill)
    _apply_text_only_redactions(page)
    _repair_foreign_spans(doc, page_index, foreign, member_bboxes)
    _remove_member_underlines(page, para.spans)
    comment_guard.restore()
    new_bbox = None
    if has_text:
        for i, line in enumerate(lines):
            _insert_line(page, origin_x + line_shift(line), first_baseline + i * pitch_val, line)
        new_bbox = (
            origin_x,
            first_baseline - ascent,
            origin_x + wrap,
            first_baseline + (len(lines) - 1) * pitch_val + descent,
        )
    return ParagraphReplaceResult(
        inserted=has_text,
        used_font=_dominant_label(runs),
        exact_font=exact_font,
        uniform_style=para.uniform_style,
        resized=resized,
        new_bbox=new_bbox,
    )


def insert_new_text(
    doc: pymupdf.Document,
    page_index: int,
    point: tuple[float, float],
    text: str,
    *,
    style: TextStyle | None = None,
) -> None:
    """Insert NEW text at a baseline point (unrotated page space).

    Additive — nothing is redacted. ``\\n`` starts extra lines below.
    ``style`` defaults to non-embedded 11pt black helv; an explicit fontfile
    style embeds a subset of the chosen system font.
    """
    style = style or TextStyle()
    insert_new_runs(doc, page_index, point, [StyledRun(text, style)])


def insert_new_runs(
    doc: pymupdf.Document,
    page_index: int,
    point: tuple[float, float],
    runs: list[StyledRun],
) -> None:
    """Insert NEW rich text at a baseline point (E9). Additive.

    Hard ``\\n`` breaks only (no wrap width for free-standing text); line
    pitch = 1.2 × the first run's base size. The point (and every line's
    baseline) is validated against the unrotated page bounds — PyMuPDF's
    ``insert_text`` accepts off-page points without complaint.
    """
    if not _runs_have_text(runs):
        raise ValueError("no text to insert")
    page = doc[page_index]
    width, height = page.rect.width, page.rect.height
    if page.rotation % 180 == 90:
        width, height = height, width
    x, y = point
    if not (0 <= x < width and 0 < y <= height):
        raise ValueError("insertion point is outside the page")

    lines = _layout_runs(runs, None)
    while lines and not lines[-1]:
        lines.pop()
    base_size = next(
        (frag.style.size for line in lines for frag in line if frag.text.strip()), 11.0
    )
    pitch = 1.2 * base_size
    if y + (len(lines) - 1) * pitch > height:
        raise ValueError("the text runs off the page bottom — remove some lines")
    for i, line in enumerate(lines):
        _insert_line(page, x, y + i * pitch, line)


def add_highlight(doc: pymupdf.Document, page_index: int, span: TextSpan) -> None:
    """Highlight one span (a standard PDF highlight annotation, E7).

    Additive and non-destructive — the annotation sits over the text. The
    page must stay referenced while the annot is touched: a ``doc[n]``
    temporary gets garbage-collected and orphans the annotation ("annotation
    not bound to any page").
    """
    page = doc[page_index]
    annot = page.add_highlight_annot(pymupdf.Rect(span.bbox))
    annot.update()


def highlight_region(
    doc: pymupdf.Document, page_index: int, rect: tuple[float, float, float, float]
) -> int:
    """Highlight the text inside a selection window (E9.1). Returns the
    number of highlight annotations added.

    Character-level clipping: a line only partly covered by the window gets
    highlighted only where its character CENTRES fall inside the rect — one
    annotation per contiguous stretch per line, so the marks hug the text
    like a real marker instead of one box over whitespace. Whitespace-only
    stretches are skipped. Same page-reference gotcha as add_highlight.
    """
    page = doc[page_index]
    region = pymupdf.Rect(rect)
    count = 0
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", ()):
            for raw_span in line["spans"]:
                stretch: list = []
                stretch_text: list[str] = []

                def flush() -> None:
                    nonlocal count, stretch, stretch_text
                    if stretch and "".join(stretch_text).strip():
                        union = pymupdf.Rect(stretch[0])
                        for bb in stretch[1:]:
                            union |= pymupdf.Rect(bb)
                        annot = page.add_highlight_annot(union)
                        annot.update()
                        count += 1
                    stretch, stretch_text = [], []

                for char in raw_span["chars"]:
                    bb = char["bbox"]
                    centre_x = (bb[0] + bb[2]) / 2
                    centre_y = (bb[1] + bb[3]) / 2
                    if region.x0 <= centre_x <= region.x1 and region.y0 <= centre_y <= region.y1:
                        stretch.append(bb)
                        stretch_text.append(char["c"])
                    else:
                        flush()
                flush()
    return count


def _embedded_font_map(doc: pymupdf.Document, page_index: int) -> dict[str, bool]:
    """basefont name (subset prefix stripped) -> is-embedded, for one page.

    ``get_page_fonts`` rows are ``(xref, ext, type, basefont, name, encoding)``;
    ``ext == "n/a"`` means the font is referenced, not embedded.

    One stripped name can appear as BOTH an embedded subset and a non-embedded
    font on the same page (common after merges). Span font names carry no
    subset prefix, so nothing span-side can break the tie — resolve collisions
    conservatively: any embedded row marks the name embedded (flagging a
    mappable span beats silently exact-matching an embedded one).
    """
    mapping: dict[str, bool] = {}
    for entry in doc.get_page_fonts(page_index):
        ext, basefont = entry[1], entry[3]
        name = strip_subset_prefix(basefont)
        mapping[name] = mapping.get(name, False) or ext != "n/a"
    return mapping
