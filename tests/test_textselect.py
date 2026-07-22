"""Window/marquee text selection engine (X3).

A selection is GEOMETRIC — every word whose centre is inside the dragged
rectangle, grouped into reading-order lines. Synthetic word boxes for the
window logic, plus real-document checks (the PdfDocument wrapper, comment
exclusion, a two-column table, rotation-blindness).
"""

from __future__ import annotations

import pymupdf

from pdfcore.document import PdfDocument
from pdfcore.textselect import (
    page_lines,
    region_rects,
    region_text,
    word_at,
    word_region_at,
    words_in_rect,
)
from pdfcore.textsource import Word, group_lines


def W(text: str, x0: float, y0: float, w: float = 40.0, h: float = 10.0) -> Word:
    return Word(text=text, bbox=(x0, y0, x0 + w, y0 + h))


# --- words_in_rect ----------------------------------------------------------


def test_selects_words_whose_centre_is_inside_the_box():
    a, b = W("alpha", 10, 100), W("beta", 100, 100)  # centres (30,105) / (120,105)
    lines = group_lines([a, b])
    region = words_in_rect(lines, (0, 90, 60, 120))  # box covers alpha only
    assert region_text(region) == "alpha"
    assert region_rects(region) == [a.bbox]


def test_box_over_a_column_takes_only_that_column():
    a1, r1 = W("left1", 10, 100), W("right1", 200, 100)
    a2, r2 = W("left2", 10, 120), W("right2", 200, 120)
    lines = group_lines([a1, r1, a2, r2])
    left = words_in_rect(lines, (0, 90, 60, 130))  # tall narrow box, left column
    assert region_text(left) == "left1\nleft2"
    everything = words_in_rect(lines, (0, 90, 260, 130))  # wide box, both columns
    assert region_text(everything) == "left1 right1\nleft2 right2"


def test_box_corner_order_does_not_matter():
    a = W("alpha", 10, 100)
    lines = group_lines([a])
    assert words_in_rect(lines, (60, 120, 0, 90)) == words_in_rect(lines, (0, 90, 60, 120))


def test_empty_selection_when_the_box_misses_all_text():
    lines = group_lines([W("alpha", 10, 100)])
    assert words_in_rect(lines, (500, 500, 600, 600)) == []
    assert region_text([]) == ""
    assert region_rects([]) == []


def test_region_rects_are_per_line_unions():
    a, b = W("alpha", 10, 100), W("beta", 100, 100)
    c = W("gamma", 10, 120)
    lines = group_lines([a, b, c])
    region = words_in_rect(lines, (0, 90, 200, 130))
    assert region_rects(region) == [(10.0, 100.0, 140.0, 110.0), (10.0, 120.0, 50.0, 130.0)]


# --- word_at / word_region_at (double-click) --------------------------------


def test_word_region_at_selects_a_single_word():
    a, b = W("alpha", 10, 100), W("beta", 100, 100)
    lines = group_lines([a, b])
    assert region_text(word_region_at(lines, 30.0, 105.0)) == "alpha"
    assert word_region_at(lines, 300.0, 300.0) is None


def test_word_at_hits_and_misses():
    a, b = W("alpha", 10, 100), W("beta", 200, 100)
    lines = group_lines([a, b])
    assert word_at(lines, 30.0, 105.0) == (0, 0)
    assert word_at(lines, 120.0, 105.0) is None  # in the gap between them


# --- real documents ---------------------------------------------------------


def _two_column_pdf(tmp_path):
    """A multi-line 'description' cell beside a separate right column on shared
    baselines — the table hazard from the user's screenshot."""
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


def test_window_selects_one_column_of_a_table(tmp_path):
    with PdfDocument.open(_two_column_pdf(tmp_path)) as doc:
        lines = doc.text_lines(0)
    # A tall, narrow box over the description column.
    text = region_text(words_in_rect(lines, (60, 110, 210, 150)))
    assert "Description first line here" in text
    assert "PARTNO" not in text and "1,185" not in text  # the column, not the row
    # A small box over the price cell alone.
    assert region_text(words_in_rect(lines, (420, 115, 470, 125))) == "1,185.47"


def test_page_lines_reads_native_words(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        lines = page_lines(doc, 0)
    assert " ".join(w.text for w in lines[0]).startswith("Heading")


def test_pdfdocument_text_lines_wrapper(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        lines = doc.text_lines(0)
    assert any("Lorem" in " ".join(w.text for w in ln) for ln in lines)


def test_comment_text_is_never_selectable(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.add_comment(0, (300.0, 500.0, 460.0, 540.0), "SECRETNOTE", author="t")
        lines = doc.text_lines(0)
    whole_page = words_in_rect(lines, (0, 0, 100000, 100000))
    assert "SECRETNOTE" not in region_text(whole_page)


def test_engine_stays_rotation_blind(text_pdf):
    """Word boxes come back in unrotated page space regardless of /Rotate —
    the UI derotates (page_coords), the engine never does (same as search)."""
    with pymupdf.open(text_pdf) as doc:
        before = [[w.bbox for w in line] for line in page_lines(doc, 0)]
        doc[0].set_rotation(90)
        after = [[w.bbox for w in line] for line in page_lines(doc, 0)]
    assert before == after
