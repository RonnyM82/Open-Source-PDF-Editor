"""Window/marquee text selection (X3): the read-only "select + copy" core.

Read-only, Acrobat-style selection over a page's NATIVE text. A selection is a
GEOMETRIC WINDOW — the user drags a rectangle and everything inside it is
selected — NOT a reading-order flow. This is deliberate (chosen after the flow
model over-reached across a quote's table columns and rows, then a
block-constrained variant became too restrictive): a marquee is predictable on
tabular documents (box a column, a cell, or a region) and needs no block
detection at all. Pure and Qt-free — every function is a plain transform of
word boxes, unit-testable without a GUI; the UI (X4) owns the gesture (a
rubber-band drag) and the on-screen chrome.

Model: a page's words grouped into reading-order lines
(:func:`textsource.group_lines`), and a SELECTION = the per-line runs of words
whose CENTRE falls inside the window (``list[list[Word]]``). Words on a line
stay in reading order; lines stay top-to-bottom, so the copied text reads
naturally (columns left-to-right, rows top-to-bottom).

Accepted limitations (documented honestly): a word is in or out by its centre —
character-level precision is out of scope. Scanned / vector-outline pages have
no native words, so there is nothing to select (same as Acrobat; no OCR in
this path). Comment/markup text is never selectable (``page_words`` drops it).
The window is geometric, so it does not follow reading order across a line
wrap the way a flow drag would — for copying (especially tables) the box is
almost always what is wanted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

from pdfcore.textsource import Word, WordLike, group_lines, page_words

# (line_index, word_index) into a grouped ``lines`` structure — points AT a
# word. Word-snapped throughout: character precision is out of scope.
Position = tuple[int, int]

# (line_index, word_index) into a grouped `lines` structure — points AT a word.
Position = tuple[int, int]
Rect = tuple[float, float, float, float]
# A grouped page: reading-order lines, each a left-to-right list of words.
Lines = Sequence[Sequence[WordLike]]
# A selection: per visual line, the reading-order run of selected words. Only
# lines with at least one selected word appear.
Region = list[list[WordLike]]


def _normalize_rect(rect: Rect) -> Rect:
    x0, y0, x1, y1 = rect
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _center_in_rect(bbox: Rect, rect: Rect) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    x0, y0, x1, y1 = rect
    return x0 <= cx <= x1 and y0 <= cy <= y1


def _words_bbox(words: Sequence[WordLike]) -> Rect:
    return (
        min(w.bbox[0] for w in words),
        min(w.bbox[1] for w in words),
        max(w.bbox[2] for w in words),
        max(w.bbox[3] for w in words),
    )


def words_in_rect(lines: Lines, rect: Rect) -> Region:
    """The selection for a window: per-line runs of words whose centre is in
    ``rect`` (any corner order). Empty lines are dropped; word and line order
    are preserved, so :func:`region_text` reads naturally.
    """
    box = _normalize_rect(rect)
    region: Region = []
    for line in lines:
        inside = [w for w in line if _center_in_rect(w.bbox, box)]
        if inside:
            region.append(inside)
    return region


def word_at(lines: Lines, px: float, py: float, pad: float = 1.0) -> Position | None:
    """The position of the word under a point (tight bbox hit, ± ``pad``).

    None when the point is not over any word; on overlapping boxes the
    smallest-area (tightest) word wins, the same tie-break as
    :func:`page_coords.span_at`. Used for double-click word selection and the
    hover I-beam.
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


def word_region_at(lines: Lines, px: float, py: float, pad: float = 1.0) -> Region | None:
    """The single word under a point AS a one-word selection (double-click),
    or None if not over a word."""
    pos = word_at(lines, px, py, pad)
    if pos is None:
        return None
    return [[lines[pos[0]][pos[1]]]]


def line_region_at(lines: Lines, px: float, py: float, pad: float = 1.0) -> Region | None:
    """The whole reading-order line under a point AS a selection (triple-click),
    or None if the point is not over any word. All the words of the line the
    hit word belongs to."""
    pos = word_at(lines, px, py, pad)
    if pos is None:
        return None
    return [list(lines[pos[0]])]


def region_text(region: Region) -> str:
    """The selected text: words space-joined, lines newline-joined (matching
    :func:`textsource.words_to_text` conventions)."""
    return "\n".join(" ".join(w.text for w in line) for line in region)


def region_rects(region: Region) -> list[Rect]:
    """Per-line union rects (unrotated page points) for the selection chrome."""
    return [_words_bbox(line) for line in region]


def page_lines(doc: pymupdf.Document, page_index: int) -> list[list[Word]]:
    """Page ``page_index``'s native words grouped into reading-order lines.

    The input to :func:`words_in_rect` / :func:`word_region_at`. Comment/markup
    text is excluded (``page_words`` drops it by default), so a review comment
    is never selectable or copyable.
    """
    return group_lines(page_words(doc, page_index))


# --- flow (run) selection ------------------------------------------------------
#
# Restored from the pre-marquee engine (X3/X3.1) for the Hyperlink tool ONLY:
# linking wants "drag a run of prose", which a geometric marquee cannot express
# (it picks words by their centre, so edge words drop out of the selection).
# The read-only copy selection deliberately stays a MARQUEE — the model chosen
# at X3.2 because flow over-reached across a quote's table columns. The two
# selection models coexist on purpose; do not merge them.


@dataclass(frozen=True)
class Selection:
    """A normalized flow range: ``start`` <= ``end`` in reading order.

    Both positions are inclusive and point at a word. ``start`` never comes
    after ``end`` (:func:`selection_span` swaps a backward drag), so callers
    can iterate ``start[0]`` → ``end[0]`` directly.
    """

    start: Position
    end: Position


def _line_vdist(line: Sequence[WordLike], py: float) -> float:
    """Vertical distance from ``py`` to a line's y-extent (0 when inside)."""
    top = min(w.bbox[1] for w in line)
    bottom = max(w.bbox[3] for w in line)
    if py < top:
        return top - py
    return py - bottom if py > bottom else 0.0


def _word_hdist(word: WordLike, px: float) -> float:
    """Horizontal distance from ``px`` to a word's x-extent (0 when inside)."""
    x0, x1 = word.bbox[0], word.bbox[2]
    if px < x0:
        return x0 - px
    return px - x1 if px > x1 else 0.0


def position_at(lines: Lines, px: float, py: float) -> Position | None:
    """Nearest text position to a page point, caret style; None if no words.

    Picks the line by vertical distance, then clamps horizontally to the line's
    ends — a point left of a line resolves to its first word, right of it to
    its last. Unlike :func:`word_at` this never returns None for a point that
    misses every word, which is what lets a drag past the end of a line keep
    extending the selection.
    """
    if not lines:
        return None
    line_index = min(range(len(lines)), key=lambda i: _line_vdist(lines[i], py))
    line = lines[line_index]
    return (line_index, min(range(len(line)), key=lambda j: _word_hdist(line[j], px)))


def _clamp_position(lines: Lines, pos: Position) -> Position:
    line_index = max(0, min(pos[0], len(lines) - 1))
    return (line_index, max(0, min(pos[1], len(lines[line_index]) - 1)))


def selection_span(
    lines: Lines, anchor_pos: Position | None, cursor_pos: Position | None
) -> Selection | None:
    """The flow range between two positions (None if either is None).

    A backward drag (anchor after cursor) is swapped, so it yields exactly the
    same selection as the equivalent forward drag.
    """
    if anchor_pos is None or cursor_pos is None or not lines:
        return None
    start, end = sorted((_clamp_position(lines, anchor_pos), _clamp_position(lines, cursor_pos)))
    return Selection(start=start, end=end)


def _word_range(lines: Lines, span: Selection, line_index: int) -> tuple[int, int]:
    """Inclusive ``(first, last)`` word indices selected on one line."""
    last_word = len(lines[line_index]) - 1
    if span.start[0] == span.end[0]:
        return (span.start[1], span.end[1])
    if line_index == span.start[0]:
        return (span.start[1], last_word)
    if line_index == span.end[0]:
        return (0, span.end[1])
    return (0, last_word)


def selection_words(lines: Lines, span: Selection | None) -> Region:
    """The selected words as a Region (per visual line), so a flow selection
    feeds the same helpers a marquee Region does."""
    if span is None:
        return []
    out: Region = []
    for line_index in range(span.start[0], span.end[0] + 1):
        first, last = _word_range(lines, span, line_index)
        words = list(lines[line_index][first : last + 1])
        if words:
            out.append(words)
    return out


def selection_rects(lines: Lines, span: Selection | None) -> list[Rect]:
    """Per-line union rects for the selected words (unrotated page points)."""
    return [_words_bbox(words) for words in selection_words(lines, span)]


def selection_text(lines: Lines, span: Selection | None) -> str:
    """The selected text: words space-joined, lines newline-joined."""
    return region_text(selection_words(lines, span))


# A word ending a sentence (trailing quotes/brackets tolerated).
_SENTENCE_END = (".", "!", "?")
# A line gap this much larger than the line height reads as a new paragraph, so
# a sentence walk never runs out of its own block.
_PARA_GAP = 1.7


def _ends_sentence(word: WordLike) -> bool:
    return word.text.rstrip("\"')]}").endswith(_SENTENCE_END)


def _step(lines: Lines, pos: Position, delta: int) -> Position | None:
    """The next/previous position in reading-order flow, or None at the ends."""
    line_index, word_index = pos
    word_index += delta
    if 0 <= word_index < len(lines[line_index]):
        return (line_index, word_index)
    line_index += delta
    if not (0 <= line_index < len(lines)) or not lines[line_index]:
        return None
    return (line_index, 0 if delta > 0 else len(lines[line_index]) - 1)


def _paragraph_break(lines: Lines, a: Position, b: Position) -> bool:
    """True when two positions sit on lines separated by a paragraph-sized gap."""
    if a[0] == b[0]:
        return False
    first, second = sorted((a[0], b[0]))
    bottom = max(w.bbox[3] for w in lines[first])
    top = min(w.bbox[1] for w in lines[second])
    height = max(w.bbox[3] - w.bbox[1] for w in lines[first]) or 1.0
    return (top - bottom) > height * _PARA_GAP


def sentence_span(lines: Lines, px: float, py: float, pad: float = 1.0) -> Selection | None:
    """The whole SENTENCE around a point (triple-click), or None if not on a word.

    Walks the reading-order flow out from the hit word — back to just after the
    previous sentence terminator, forward through the next one — following a
    sentence across wrapped lines but never across a paragraph break.
    """
    pos = word_at(lines, px, py, pad)
    if pos is None:
        return None
    start = pos
    while (prev := _step(lines, start, -1)) is not None:
        if _paragraph_break(lines, prev, start) or _ends_sentence(lines[prev[0]][prev[1]]):
            break
        start = prev
    end = pos
    while not _ends_sentence(lines[end[0]][end[1]]):
        nxt = _step(lines, end, 1)
        if nxt is None or _paragraph_break(lines, end, nxt):
            break
        end = nxt
    return Selection(start=start, end=end)
