"""Word-snapped flow text selection engine (X3).

Pure — synthetic word boxes for the flow logic, plus a couple of real-document
checks (the PdfDocument wrapper, comment exclusion, rotation-blindness). The
selection follows READING-ORDER LINES, never a geometry rectangle: the
two-column fixture below is the proof.
"""

from __future__ import annotations

import pymupdf

from pdfcore.document import PdfDocument
from pdfcore.textselect import (
    Selection,
    page_lines,
    position_at,
    selection_rects,
    selection_span,
    selection_text,
    word_at,
)
from pdfcore.textsource import Word, group_lines


def W(text: str, x0: float, y0: float, w: float = 40.0, h: float = 10.0) -> Word:
    return Word(text=text, bbox=(x0, y0, x0 + w, y0 + h))


def _center(word: Word) -> tuple[float, float]:
    x0, y0, x1, y1 = word.bbox
    return ((x0 + x1) / 2, (y0 + y1) / 2)


# --- position_at / word_at --------------------------------------------------


def test_position_at_none_on_empty_page():
    assert position_at([], 10.0, 10.0) is None


def test_position_at_picks_nearest_line_then_word():
    a, b = W("alpha", 10, 100), W("beta", 60, 100)
    c, d = W("gamma", 10, 140), W("delta", 60, 140)
    lines = group_lines([a, b, c, d])
    # A point on the lower line, under the second word.
    assert position_at(lines, *_center(d)) == (1, 1)
    assert position_at(lines, *_center(a)) == (0, 0)


def test_position_at_clamps_past_line_ends():
    a, b = W("alpha", 10, 100), W("beta", 60, 100)
    lines = group_lines([a, b])
    assert position_at(lines, -500.0, 105.0) == (0, 0)  # far left -> first word
    assert position_at(lines, 5000.0, 105.0) == (0, 1)  # far right -> last word


def test_word_at_hits_tight_and_misses_between():
    a, b = W("alpha", 10, 100), W("beta", 200, 100)
    lines = group_lines([a, b])
    assert word_at(lines, *_center(a)) == (0, 0)
    assert word_at(lines, *_center(b)) == (0, 1)
    assert word_at(lines, 120.0, 105.0) is None  # in the gap between them
    assert word_at(lines, 10.0, 500.0) is None  # nowhere near a word


def test_word_at_smallest_area_wins_on_overlap():
    big = W("BIG", 10, 100, w=100, h=40)
    small = W("small", 40, 110, w=20, h=10)
    lines = [[big, small]]  # deliberate overlap; group_lines would keep order
    assert word_at(lines, 50.0, 115.0) == (0, 1)  # the tighter box


# --- single word / single line ----------------------------------------------


def test_single_word_selection():
    a = W("solo", 10, 100)
    lines = group_lines([a])
    pos = word_at(lines, *_center(a))
    span = selection_span(lines, pos, pos)
    assert selection_text(lines, span) == "solo"
    assert selection_rects(lines, span) == [a.bbox]


def test_single_line_selection_spans_words():
    a, b, c = W("one", 10, 100), W("two", 60, 100), W("three", 110, 100)
    lines = group_lines([a, b, c])
    span = selection_span(lines, (0, 0), (0, 2))
    assert selection_text(lines, span) == "one two three"
    (rect,) = selection_rects(lines, span)
    assert rect == (10.0, 100.0, 150.0, 110.0)  # union of all three


# --- multi-line flow --------------------------------------------------------


def _two_column_lines():
    """Three reading-order lines of two words each — the flow-vs-rectangle
    proof. A rectangle from ``beta`` down to ``gamma`` would also enclose
    ``alpha`` and ``delta``; the FLOW range must not."""
    alpha, beta = W("alpha", 10, 100), W("beta", 100, 100)
    gamma, delta = W("gamma", 10, 120), W("delta", 100, 120)
    epsilon, zeta = W("epsilon", 10, 140), W("zeta", 100, 140)
    words = [alpha, beta, gamma, delta, epsilon, zeta]
    return group_lines(words), words


def test_multi_line_flow_forward():
    lines, _ = _two_column_lines()
    span = selection_span(lines, (0, 0), (2, 0))  # alpha -> epsilon
    assert selection_text(lines, span) == "alpha beta\ngamma delta\nepsilon"
    assert len(selection_rects(lines, span)) == 3


def test_backward_drag_equals_forward_drag():
    lines, _ = _two_column_lines()
    forward = selection_span(lines, (0, 1), (2, 0))
    backward = selection_span(lines, (2, 0), (0, 1))
    assert forward == backward
    assert selection_text(lines, forward) == selection_text(lines, backward)


def test_flow_follows_line_order_not_geometry_rectangle():
    lines, (alpha, beta, gamma, delta, _e, _z) = _two_column_lines()
    span = selection_span(lines, (0, 1), (1, 0))  # beta -> gamma
    text = selection_text(lines, span)
    assert text == "beta\ngamma"
    # A geometry rectangle from beta to gamma would swallow these; flow doesn't.
    assert "alpha" not in text
    assert "delta" not in text
    # Two per-line rects, each hugging ONE word — not one enclosing box.
    rects = selection_rects(lines, span)
    assert rects == [beta.bbox, gamma.bbox]


def test_selection_rects_are_per_line_unions():
    lines, _ = _two_column_lines()
    span = selection_span(lines, (0, 0), (1, 1))  # whole first two lines
    r0, r1 = selection_rects(lines, span)
    assert r0 == (10.0, 100.0, 140.0, 110.0)  # alpha..beta union
    assert r1 == (10.0, 120.0, 140.0, 130.0)  # gamma..delta union


# --- edge cases -------------------------------------------------------------


def test_selection_span_none_when_a_position_is_none():
    lines, _ = _two_column_lines()
    assert selection_span(lines, None, (0, 0)) is None
    assert selection_span(lines, (0, 0), None) is None


def test_selection_helpers_none_span_are_empty():
    lines, _ = _two_column_lines()
    assert selection_rects(lines, None) == []
    assert selection_text(lines, None) == ""


def test_selection_is_a_frozen_normalized_dataclass():
    lines, _ = _two_column_lines()
    span = selection_span(lines, (2, 1), (0, 0))
    assert isinstance(span, Selection)
    assert span.start == (0, 0) and span.end == (2, 1)


# --- real-document wiring ----------------------------------------------------


def test_page_lines_reads_native_words(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        lines = page_lines(doc, 0)
    assert lines  # non-empty
    texts = [w.text for line in lines for w in line]
    assert "Heading" in texts
    # First line is the heading (top of the page).
    assert " ".join(w.text for w in lines[0]).startswith("Heading")


def test_pdfdocument_text_lines_wrapper_selects_a_body_line(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        lines = doc.text_lines(0)
        # Find the line that is the first body line ("Lorem ipsum ... line 0.").
        idx = next(i for i, ln in enumerate(lines) if "line" in " ".join(w.text for w in ln))
        span = selection_span(lines, (idx, 0), (idx, len(lines[idx]) - 1))
        assert selection_text(lines, span).startswith("Lorem ipsum")


def test_whole_page_selection_matches_reading_order(multipage_pdf):
    with PdfDocument.open(multipage_pdf) as doc:
        lines = doc.text_lines(2)  # page carries PAGE-MARKER-002
    span = selection_span(lines, (0, 0), (len(lines) - 1, len(lines[-1]) - 1))
    assert "PAGE-MARKER-002" in selection_text(lines, span)


def test_comment_text_is_never_selectable(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.add_comment(0, (300.0, 500.0, 460.0, 540.0), "SECRET REVIEW NOTE", author="tester")
        lines = doc.text_lines(0)
    span = selection_span(lines, (0, 0), (len(lines) - 1, len(lines[-1]) - 1))
    assert "SECRET" not in selection_text(lines, span)


# --- block-constrained selection (X3.1: contain a drag within one block) -----


def _two_column_pdf(tmp_path):
    """A multi-line 'description' cell beside a separate right-hand column on
    the SAME baselines — the table hazard. Tight line pitch makes MuPDF group
    the 3 description lines into ONE block and keep the right column separate
    (probe-verified), mirroring the real quote."""
    path = tmp_path / "twocol.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for i, text in enumerate(
        ["Description first line here", "Description second line more", "Third and final line"]
    ):
        page.insert_text((72, 120 + i * 8), text, fontname="helv", fontsize=7)
    page.insert_text((300, 120), "PARTNO-123", fontname="helv", fontsize=7)
    page.insert_text((430, 120), "1,185.47", fontname="helv", fontsize=7)
    doc.save(str(path))
    doc.close()
    return path


def test_block_lines_at_returns_only_the_column_paragraph(tmp_path):
    with PdfDocument.open(_two_column_pdf(tmp_path)) as doc:
        lines = doc.text_block_lines_at(0, 90.0, 121.0)  # inside the description
    texts = [" ".join(w.text for w in ln) for ln in lines]
    assert texts == [
        "Description first line here",
        "Description second line more",
        "Third and final line",
    ]
    flat = [w.text for ln in lines for w in ln]
    assert "PARTNO-123" not in flat and "1,185.47" not in flat  # the column, not the row


def test_block_lines_at_none_off_text(tmp_path):
    with PdfDocument.open(_two_column_pdf(tmp_path)) as doc:
        assert doc.text_block_lines_at(0, 400.0, 500.0) is None  # blank area


def test_selection_clamps_to_the_block_dragged_from(tmp_path):
    """A drag anchored in the description column, dragged far into the right
    column, stays inside the description — the containment the user asked for."""
    with PdfDocument.open(_two_column_pdf(tmp_path)) as doc:
        lines = doc.text_block_lines_at(0, 74.0, 121.0)
        anchor = position_at(lines, 74.0, 121.0)
        cursor = position_at(lines, 460.0, 137.0)  # bottom-right, off the block
        span = selection_span(lines, anchor, cursor)
        text = selection_text(lines, span)
    assert "PARTNO" not in text and "1,185" not in text
    assert text.startswith("Description first line here")


def test_same_baseline_neighbours_are_separate_blocks(tmp_path):
    """Cells on the SAME baseline are separate blocks — a paragraph is a
    VERTICAL run of lines, never horizontal neighbours (the CAD/title-block
    rule). So selecting one cell never grabs its row-mates or the other
    column."""
    with PdfDocument.open(_two_column_pdf(tmp_path)) as doc:
        lines = doc.text_block_lines_at(0, 310.0, 121.0)  # the PARTNO cell
    flat = [w.text for ln in lines for w in ln]
    assert "PARTNO-123" in flat
    assert "1,185.47" not in flat  # same-baseline neighbour is its own block
    assert "Description" not in flat


def test_block_lines_at_excludes_comment_text(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.add_comment(0, (300.0, 500.0, 460.0, 540.0), "SECRETNOTE", author="t")
        # A block over real text still resolves; the comment's own text never does.
        span = next(s for s in doc.text_spans(0) if s.text.strip() == quote_pdf.price)
        cx, cy = (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
        lines = doc.text_block_lines_at(0, cx, cy)
        # And a block resolved AT the comment's location is None (markup only).
        at_comment = doc.text_block_lines_at(0, 380.0, 520.0)
    flat = [w.text for ln in (lines or []) for w in ln]
    assert "SECRETNOTE" not in flat
    assert at_comment is None


def test_engine_stays_rotation_blind(text_pdf):
    """Word boxes come back in unrotated page space regardless of /Rotate —
    the UI derotates (page_coords), the engine never does (same as search)."""
    with pymupdf.open(text_pdf) as doc:
        before = [[w.bbox for w in line] for line in page_lines(doc, 0)]
        doc[0].set_rotation(90)
        after = [[w.bbox for w in line] for line in page_lines(doc, 0)]
    assert before == after
