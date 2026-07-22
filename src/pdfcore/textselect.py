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

import pymupdf

from pdfcore.textsource import Word, WordLike, group_lines, page_words

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
