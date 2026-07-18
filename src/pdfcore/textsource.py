"""Shared text sourcing: native-vs-OCR routing for search and Extract Text (X0).

READ-ONLY path — never touches the edit pipeline. Every function is stateless
and Qt-free; OCR words are always CALLER-supplied (the UI owns the per-page
OCR cache — CLAUDE.md rule 8: the engine never caches and never hides a slow
OCR call inside an innocuous-looking text getter). All coordinates are
unrotated page points: ``get_text("words")`` rects and
:class:`~pdfcore.ocr.OcrWord` bboxes share that space, so both feed the same
line grouping and (from SR1) the same search matcher.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

from pdfcore.ocr import OcrWord


@dataclass(frozen=True)
class Word:
    """A native text-layer word (duck-compatible with OcrWord: .text/.bbox)."""

    text: str
    bbox: tuple[float, float, float, float]


# Anything with .text and .bbox — native Word or recognised OcrWord.
WordLike = Word | OcrWord


@dataclass(frozen=True)
class PageText:
    """One page's text and where it came from.

    ``source``: ``"native"`` (text layer), ``"ocr"`` (recognised words) or
    ``"empty"`` (neither yielded anything). ``ocr_attempted`` splits the empty
    case for honest reporting: True = OCR ran and found nothing; False = OCR
    was not attempted (native page, no tesseract, or the user declined).
    """

    source: str
    text: str
    ocr_attempted: bool = False


def group_lines(words: Sequence[WordLike]) -> list[list[WordLike]]:
    """Group words into visual lines (y-overlap >= 50% of the smaller height),
    each line sorted by x, lines sorted top to bottom.

    Lifted verbatim from invoice.py at X0 — invoice imports it from here, so
    the O-series field extraction and these read features share ONE line rule.
    """
    lines: list[list[WordLike]] = []
    for word in sorted(words, key=lambda w: w.bbox[1]):
        for line in lines:
            top = max(word.bbox[1], min(w.bbox[1] for w in line))
            bottom = min(word.bbox[3], max(w.bbox[3] for w in line))
            smallest = min(word.bbox[3] - word.bbox[1], min(w.bbox[3] - w.bbox[1] for w in line))
            if bottom - top > 0.5 * smallest:
                line.append(word)
                break
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda w: w.bbox[0])
    lines.sort(key=lambda line: min(w.bbox[1] for w in line))
    return lines


def words_to_text(words: Sequence[WordLike]) -> str:
    """Reading-order text: lines newline-joined, words space-joined."""
    return "\n".join(" ".join(w.text for w in line) for line in group_lines(words))


def page_words(
    doc: pymupdf.Document, page_index: int, include_comments: bool = False
) -> list[Word]:
    """The page's native text-layer words (unrotated page points).

    Review-comment annotation text LEAKS into ``get_text("words")`` (E11
    probe), so comment words are dropped by default — Extract Text always
    excludes them (user decision) and search includes them only via its
    opt-in checkbox.
    """
    words = [
        Word(text=row[4], bbox=(row[0], row[1], row[2], row[3]))
        for row in doc[page_index].get_text("words")
    ]
    if include_comments:
        return words
    from pdfcore import comments as comments_module

    rects = comments_module.comment_rects(doc, page_index)
    if not rects:
        return words
    return [
        w
        for w in words
        if not any(
            r[0] <= (w.bbox[0] + w.bbox[2]) / 2 <= r[2]
            and r[1] <= (w.bbox[1] + w.bbox[3]) / 2 <= r[3]
            for r in rects
        )
    ]


def has_text_layer(doc: pymupdf.Document, page_index: int) -> bool:
    """True when the page has extractable native text.

    Checked via ``get_text("words")`` — the exact primitive the native route
    consumes, so "has a layer" is by definition "the native route yields
    words" (a "blocks" check would need image-block filtering; raw text
    length counts whitespace-only layers).
    """
    return bool(doc[page_index].get_text("words"))


def collect_page_text(
    doc: pymupdf.Document,
    page_index: int,
    ocr_words: Sequence[OcrWord] | None = None,
) -> PageText:
    """Route one page's text: native when a layer exists, else caller's OCR.

    Both branches assemble text through the SAME line grouping, so native and
    OCR pages read consistently. ``ocr_words=None`` means OCR was not
    attempted; ``[]`` means it ran and found nothing — the distinction feeds
    the honest empty-page wording in the extract output.
    """
    native = page_words(doc, page_index)
    if native:
        return PageText(source="native", text=words_to_text(native))
    if ocr_words:
        return PageText(source="ocr", text=words_to_text(ocr_words), ocr_attempted=True)
    return PageText(source="empty", text="", ocr_attempted=ocr_words is not None)


Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class SearchHit:
    """One occurrence of the query: its page and one rect PER LINE FRAGMENT.

    A match that wraps across a line break cannot be one rectangle without
    covering unrelated text between the lines — so ``rects`` carries one
    union rect per visual line the match touches. One hit = one occurrence,
    however many rects it draws ("3 of 17" counts hits, never rects).
    """

    page_index: int
    rects: tuple[Rect, ...]


def search_words(words: Sequence[WordLike], query: str) -> list[tuple[Rect, ...]]:
    """Find ``query`` in ``words``; one entry per occurrence, reading order.

    ALWAYS case-insensitive — by design (user decision 2026-07-04), there is
    no case toggle anywhere; both sides are lower-cased per word BEFORE
    joining, and the offset map is built from the lower-cased lengths, so
    offsets cannot desynchronise. The query's whitespace is collapsed; words
    are joined with single spaces in reading order (line breaks become
    spaces, so phrases match across a wrap). Matches are non-overlapping
    (scanning resumes at each match's end). Hit rects are word-granular:
    a mid-word match covers its whole word. Works identically for native
    ``Word`` and recognised ``OcrWord`` input — the ONE matcher for both.
    """
    needle = " ".join(query.lower().split())
    if not needle:
        return []
    # Haystack + per-word offset spans, built from the SAME lower-cased text.
    pieces: list[str] = []
    spans: list[tuple[int, int, int, WordLike]] = []  # (start, end, line_idx, word)
    pos = 0
    for line_idx, line in enumerate(group_lines(words)):
        for word in line:
            lowered = word.text.lower()
            if pieces:
                pos += 1  # the joining space
            spans.append((pos, pos + len(lowered), line_idx, word))
            pieces.append(lowered)
            pos += len(lowered)
    haystack = " ".join(pieces)

    hits: list[tuple[Rect, ...]] = []
    at = haystack.find(needle)
    while at != -1:
        end = at + len(needle)
        by_line: dict[int, list[WordLike]] = {}
        for start, stop, line_idx, word in spans:
            if start < end and stop > at:  # word overlaps the match
                by_line.setdefault(line_idx, []).append(word)
        hits.append(
            tuple(
                (
                    min(w.bbox[0] for w in ws),
                    min(w.bbox[1] for w in ws),
                    max(w.bbox[2] for w in ws),
                    max(w.bbox[3] for w in ws),
                )
                for _, ws in sorted(by_line.items())
            )
        )
        at = haystack.find(needle, end)
    return hits


def search_page(
    doc: pymupdf.Document, page_index: int, query: str, include_comments: bool = False
) -> list[tuple[Rect, ...]]:
    """Search one page's NATIVE text layer (unrotated page-point rects)."""
    return search_words(page_words(doc, page_index, include_comments=include_comments), query)


def search_document(
    doc: pymupdf.Document, query: str, include_comments: bool = False
) -> list[SearchHit]:
    """Search every page's native text layer; hits in (page, reading) order.

    ``include_comments`` extends the search into review-comment text (the
    search bar's opt-in checkbox — OFF by default, user decision)."""
    hits: list[SearchHit] = []
    for n in range(doc.page_count):
        hits.extend(
            SearchHit(page_index=n, rects=rects)
            for rects in search_page(doc, n, query, include_comments=include_comments)
        )
    return hits


EMPTY_AFTER_OCR = "[No text was found on this page — it has no text layer and OCR found nothing.]"
EMPTY_NO_OCR = "[No text was found on this page — it has no text layer and OCR was not attempted.]"


def format_extracted_text(sections: Sequence[tuple[int, PageText]]) -> str:
    """Human-readable extract: per-page sections labelled by text source.

    Page numbers are 1-based. Empty pages get an explicit sentence — never a
    silent blank — with the wording split on whether OCR actually ran.
    """
    parts: list[str] = []
    for page_index, page in sections:
        number = page_index + 1
        if page.source == "native":
            parts.append(f"=== Page {number} — text layer ===\n{page.text}")
        elif page.source == "ocr":
            parts.append(f"=== Page {number} — OCR (no text layer) ===\n{page.text}")
        else:
            sentence = EMPTY_AFTER_OCR if page.ocr_attempted else EMPTY_NO_OCR
            parts.append(f"=== Page {number} ===\n{sentence}")
    return "\n\n".join(parts)
