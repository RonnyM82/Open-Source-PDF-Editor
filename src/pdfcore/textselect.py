"""Word-snapped flow text selection (X3): the read-only "select + copy" core.

Acrobat-style selection over a page's NATIVE text. Built on the established
word + line-ordering primitives (:func:`textsource.page_words` and
:func:`textsource.group_lines`) so there is ONE line rule everywhere. Pure and
Qt-free — every function is a plain transform of word boxes, unit-testable
without a GUI; the UI (X4) owns the gestures and the on-screen chrome.

Model: a page's words grouped into reading-order lines, and a text POSITION =
``(line_index, word_index)`` pointing AT a word (both indices into the
``lines`` structure ``group_lines`` returns). A selection is the FLOW range
between two positions — anchor word to the end of its line, whole intermediate
lines, then the start of the last line up to the cursor word. That is NOT a
rectangular window (the highlight feature is); a range that starts in a left
column and ends lower-right follows READING ORDER, it does not box a region.

Accepted limitations (documented honestly): selection is WORD-SNAPPED —
endpoints snap to whole words, character-level precision is out of scope.
Scanned / vector-outline pages have no native words, so there is nothing to
select (same as Acrobat; no OCR in this path). Rotated text (e.g. CAD labels)
whose glyphs do not share a horizontal baseline groups as isolated words rather
than flowing lines — those behave as single-word units.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

from pdfcore.textsource import Word, WordLike, group_lines, page_words

# (line_index, word_index) into a grouped `lines` structure — points AT a word.
Position = tuple[int, int]
Rect = tuple[float, float, float, float]
# A grouped page: reading-order lines, each a left-to-right list of words.
Lines = Sequence[Sequence[WordLike]]


@dataclass(frozen=True)
class Selection:
    """A normalized flow range: ``start`` <= ``end`` in reading order.

    Both positions are inclusive and point at a word. ``start`` never comes
    after ``end`` (:func:`selection_span` swaps a backward drag), so callers
    can iterate ``start.line`` → ``end.line`` directly.
    """

    start: Position
    end: Position


def _line_vdist(line: Sequence[WordLike], py: float) -> float:
    """Vertical distance from ``py`` to a line's y-extent (0 when inside)."""
    top = min(w.bbox[1] for w in line)
    bottom = max(w.bbox[3] for w in line)
    if py < top:
        return top - py
    if py > bottom:
        return py - bottom
    return 0.0


def _word_hdist(word: WordLike, px: float) -> float:
    """Horizontal distance from ``px`` to a word's x-extent (0 when inside)."""
    x0, x1 = word.bbox[0], word.bbox[2]
    if px < x0:
        return x0 - px
    if px > x1:
        return px - x1
    return 0.0


def position_at(lines: Lines, px: float, py: float) -> Position | None:
    """Nearest text position to a page point, caret style; None if no words.

    Picks the line by vertical distance, then clamps horizontally to the
    line's ends — a point left of a line resolves to its first word, right of
    it to its last, and inside to the nearest word. Word-snapped: the position
    always names a whole word (character precision is out of scope).
    """
    if not lines:
        return None
    line_index = min(range(len(lines)), key=lambda i: _line_vdist(lines[i], py))
    line = lines[line_index]
    word_index = min(range(len(line)), key=lambda j: _word_hdist(line[j], px))
    return (line_index, word_index)


def word_at(lines: Lines, px: float, py: float, pad: float = 1.0) -> Position | None:
    """The position of the word under a point (tight bbox hit, ± ``pad``).

    For double-click word selection. Returns None when the point is not over
    any word; on overlapping boxes the smallest-area (tightest) word wins, the
    same tie-break as :func:`page_coords.span_at`.
    """
    best: Position | None = None
    best_area = float("inf")
    for i, line in enumerate(lines):
        for j, word in enumerate(line):
            x0, y0, x1, y1 = word.bbox
            if x0 - pad <= px <= x1 + pad and y0 - pad <= py <= y1 + pad:
                area = (x1 - x0) * (y1 - y0)
                if area < best_area:
                    best, best_area = (i, j), area
    return best


def _clamp_position(lines: Lines, pos: Position) -> Position:
    """Clamp a position onto valid ``lines`` indices (defensive)."""
    line_index = max(0, min(pos[0], len(lines) - 1))
    word_index = max(0, min(pos[1], len(lines[line_index]) - 1))
    return (line_index, word_index)


def selection_span(
    lines: Lines, anchor_pos: Position | None, cursor_pos: Position | None
) -> Selection | None:
    """The flow range between two positions (None if either is None).

    Handles an anchor-after-cursor (backward) drag by swapping, so a backward
    drag yields exactly the same selection as the equivalent forward one.
    """
    if anchor_pos is None or cursor_pos is None or not lines:
        return None
    a = _clamp_position(lines, anchor_pos)
    b = _clamp_position(lines, cursor_pos)
    start, end = sorted((a, b))  # reading order: line first, then word
    return Selection(start=start, end=end)


def _word_range(lines: Lines, span: Selection, line_index: int) -> tuple[int, int]:
    """Inclusive ``(first, last)`` word indices selected on one line.

    First line: the anchor word to the line end. Last line: the start to the
    cursor word. Whole lines in between. A single-line span is the anchor
    word to the cursor word.
    """
    last_word = len(lines[line_index]) - 1
    if span.start[0] == span.end[0]:
        return (span.start[1], span.end[1])
    if line_index == span.start[0]:
        return (span.start[1], last_word)
    if line_index == span.end[0]:
        return (0, span.end[1])
    return (0, last_word)


def selection_rects(lines: Lines, span: Selection | None) -> list[Rect]:
    """Per-line union rects for the selected words (unrotated page points).

    One rect per visual line the selection touches — the highlight chrome the
    UI paints. Empty when ``span`` is None.
    """
    if span is None:
        return []
    rects: list[Rect] = []
    for line_index in range(span.start[0], span.end[0] + 1):
        line = lines[line_index]
        first, last = _word_range(lines, span, line_index)
        words = line[first : last + 1]
        if not words:
            continue
        rects.append(
            (
                min(w.bbox[0] for w in words),
                min(w.bbox[1] for w in words),
                max(w.bbox[2] for w in words),
                max(w.bbox[3] for w in words),
            )
        )
    return rects


def selection_text(lines: Lines, span: Selection | None) -> str:
    """The selected text: words space-joined, lines newline-joined.

    Matches :func:`textsource.words_to_text` conventions, so a whole-line
    selection reproduces that line's extracted text exactly. Empty when
    ``span`` is None.
    """
    if span is None:
        return ""
    parts: list[str] = []
    for line_index in range(span.start[0], span.end[0] + 1):
        line = lines[line_index]
        first, last = _word_range(lines, span, line_index)
        parts.append(" ".join(w.text for w in line[first : last + 1]))
    return "\n".join(parts)


def page_lines(doc: pymupdf.Document, page_index: int) -> list[list[Word]]:
    """Page ``page_index``'s native words grouped into reading-order lines.

    Comment/markup text is excluded (``page_words`` drops it by default), so a
    review comment is never selectable or copyable.
    """
    return group_lines(page_words(doc, page_index))
