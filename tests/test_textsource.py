"""Shared text sourcing (X0): text-layer detection, line grouping, routing.

All synthetic — no tesseract in the loop (OCR words are hand-built where the
OCR branch is exercised).
"""

import pymupdf

from pdfcore import invoice, textsource
from pdfcore.document import PdfDocument
from pdfcore.ocr import OcrWord
from pdfcore.textsource import (
    Word,
    collect_page_text,
    has_text_layer,
    page_words,
    words_to_text,
)


def W(text: str, x0: float, y: float) -> Word:
    return Word(text=text, bbox=(x0, y, x0 + 10.0, y + 7.0))


def O(text: str, x0: float, y: float) -> OcrWord:  # noqa: E743 - terse test helper
    return OcrWord(text=text, bbox=(x0, y, x0 + 10.0, y + 7.0), confidence=90.0)


def test_has_text_layer_true_on_text_pdf(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        assert has_text_layer(doc, 0)
        assert has_text_layer(doc, 2)


def test_has_text_layer_false_on_rasterized(ocr_pdf):
    with pymupdf.open(ocr_pdf.path) as doc:
        assert not has_text_layer(doc, 0)


def test_group_lines_is_shared_with_invoice():
    """The lift is a pure move: invoice must use the SAME function object."""
    assert invoice._group_lines is textsource.group_lines


def test_words_to_text_orders_top_down_then_left_right():
    scrambled = [W("below", 10, 120), W("second", 40, 102), W("first", 10, 100)]
    assert words_to_text(scrambled) == "first second\nbelow"


def test_page_words_reads_native_layer(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        words = page_words(doc, 0)
    texts = [w.text for w in words]
    assert "Heading" in texts
    for word in words:
        x0, y0, x1, y1 = word.bbox
        assert x0 < x1 and y0 < y1


def test_collect_page_text_native(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        result = collect_page_text(doc, 0)
    assert result.source == "native"
    assert result.ocr_attempted is False
    assert "Heading for page 0" in result.text
    assert "line 19" in result.text


def test_collect_page_text_uses_supplied_ocr_words(ocr_pdf):
    with pymupdf.open(ocr_pdf.path) as doc:
        result = collect_page_text(doc, 0, ocr_words=[O("Hello", 10, 100), O("world", 30, 100)])
    assert result.source == "ocr"
    assert result.ocr_attempted is True
    assert result.text == "Hello world"


def test_collect_page_text_empty_after_ocr_ran(ocr_pdf):
    with pymupdf.open(ocr_pdf.path) as doc:
        result = collect_page_text(doc, 0, ocr_words=[])
    assert result == textsource.PageText(source="empty", text="", ocr_attempted=True)


def test_collect_page_text_empty_ocr_not_attempted(ocr_pdf):
    with pymupdf.open(ocr_pdf.path) as doc:
        result = collect_page_text(doc, 0, ocr_words=None)
    assert result == textsource.PageText(source="empty", text="", ocr_attempted=False)


def test_native_layer_wins_over_supplied_ocr_words(text_pdf):
    """A page WITH a text layer ignores OCR words — native text is truth."""
    with pymupdf.open(text_pdf) as doc:
        result = collect_page_text(doc, 0, ocr_words=[O("bogus", 10, 100)])
    assert result.source == "native"
    assert "bogus" not in result.text


def test_pdfdocument_wrappers(text_pdf, ocr_pdf):
    with PdfDocument.open(text_pdf) as doc:
        assert doc.has_text_layer(0)
        assert doc.page_text(0).source == "native"
    with PdfDocument.open(ocr_pdf.path) as doc:
        assert not doc.has_text_layer(0)
        assert doc.page_text(0, ocr_words=[O("Hi", 10, 100)]).source == "ocr"


# --- SR1: the search matcher (always case-insensitive, one hit = one occurrence)


def test_search_document_finds_marker_on_right_page(multipage_pdf):
    with pymupdf.open(multipage_pdf) as doc:
        hits = textsource.search_document(doc, "PAGE-MARKER-002")
    assert len(hits) == 1
    assert hits[0].page_index == 2
    assert len(hits[0].rects) == 1


def test_search_is_always_case_insensitive(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        lower = textsource.search_document(doc, "heading")
        upper = textsource.search_document(doc, "HEADING")
        exact = textsource.search_document(doc, "Heading")
    assert len(lower) == len(upper) == len(exact) == 3  # one per page
    assert lower == upper == exact


def test_search_lowercase_query_finds_uppercase_ocr_words():
    """The SR4 seam: OcrWords through the SAME matcher, case-insensitively."""
    words = [
        OcrWord(text="INV-SAMPLE-0001", bbox=(10, 100, 90, 108), confidence=90.0),
        OcrWord(text="10001", bbox=(120, 100, 150, 108), confidence=96.0),
    ]
    hits = textsource.search_words(words, "sample")
    assert len(hits) == 1
    assert hits[0] == ((10, 100, 90, 108),)


def test_search_count_equals_occurrences(text_pdf):
    """The '3 of 17' truth anchor: hit count == occurrence count."""
    with pymupdf.open(text_pdf) as doc:
        page_hits = textsource.search_page(doc, 0, "Lorem")
        doc_hits = textsource.search_document(doc, "Lorem")
    assert len(page_hits) == 20  # one per body line
    assert len(doc_hits) == 60  # three pages


def test_search_cross_line_match_is_one_hit_with_two_rects():
    words = [
        W("alpha", 10, 100),
        W("beta", 30, 100),
        W("gamma", 10, 120),
        W("delta", 30, 120),
    ]
    hits = textsource.search_words(words, "beta gamma")
    assert len(hits) == 1  # ONE occurrence...
    assert len(hits[0]) == 2  # ...drawn as one rect per line fragment
    assert hits[0][0] == (30, 100, 40, 107)  # beta's box
    assert hits[0][1] == (10, 120, 20, 127)  # gamma's box


def test_search_mid_word_substring_covers_whole_word():
    words = [W("Lorem", 10, 100)]
    hits = textsource.search_words(words, "orem")
    assert hits == [((10, 100, 20, 107),)]  # word-granular highlight


def test_search_query_whitespace_is_normalised():
    words = [W("beta", 30, 100), W("gamma", 50, 100)]
    assert textsource.search_words(words, "  beta   gamma  ") == textsource.search_words(
        words, "beta gamma"
    )


def test_search_empty_or_blank_query_finds_nothing():
    words = [W("alpha", 10, 100)]
    assert textsource.search_words(words, "") == []
    assert textsource.search_words(words, "   ") == []


def test_search_matches_do_not_overlap():
    words = [W("aaaa", 10, 100)]
    assert len(textsource.search_words(words, "aa")) == 2  # not 3


def test_search_rects_are_unrotated_page_points(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        before = textsource.search_page(doc, 0, "Heading")
        doc[0].set_rotation(90)
        after = textsource.search_page(doc, 0, "Heading")
    assert before == after  # the engine stays rotation-blind; the UI derotates


def test_search_hits_come_in_reading_order(text_pdf):
    with pymupdf.open(text_pdf) as doc:
        hits = textsource.search_document(doc, "line")
    pages = [h.page_index for h in hits]
    assert pages == sorted(pages)
    for n in set(pages):
        tops = [h.rects[0][1] for h in hits if h.page_index == n]
        assert tops == sorted(tops)


def test_pdfdocument_search_wrapper(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        hits = doc.search("Heading")
    assert [h.page_index for h in hits] == [0, 1, 2]
