"""Invoice field extraction (O3): OCR word boxes -> structured fields.

The parsing core is exercised with SYNTHETIC OcrWords (no tesseract needed —
the geometry rules are pure functions of boxes); the two end-to-end tests run
real OCR and skip without the binary / the local real sample.
"""

from decimal import Decimal

import pytest

from pdfcore import invoice, ocr
from pdfcore.document import PdfDocument
from pdfcore.invoice import extract_invoice, parse_money
from pdfcore.ocr import OcrWord

needs_tesseract = pytest.mark.skipif(
    not ocr.tesseract_available(), reason="tesseract binary not installed"
)


def W(text: str, x0: float, x1: float, y: float = 100.0, conf: float = 90.0) -> OcrWord:
    return OcrWord(text=text, bbox=(x0, y, x1, y + 7.0), confidence=conf)


# --- unit: money ---------------------------------------------------------------


def test_parse_money():
    assert parse_money("$3,600.00") == Decimal("3600.00")
    assert parse_money("327.27") == Decimal("327.27")
    assert parse_money("$0.00") == Decimal("0.00")
    assert parse_money("-5.00") == Decimal("-5.00")
    assert parse_money("$3,600") is None  # no cents -> not money-shaped
    assert parse_money("INV-SAMPLE-0001") is None
    assert parse_money("(AUD):") is None


# --- unit: line grouping ---------------------------------------------------------


def test_group_lines_by_vertical_overlap():
    a = W("first", 10, 30, y=100)
    b = W("second", 40, 70, y=102)  # overlaps a -> same line
    c = W("below", 10, 40, y=120)  # separate line
    lines = invoice._group_lines([c, b, a])
    assert [[w.text for w in line] for line in lines] == [["first", "second"], ["below"]]


# --- unit: column assignment + straddle rule -------------------------------------


def _item_columns() -> list:
    header = [
        W("Code", 99, 121),
        W("Item", 152, 170),
        W("Options", 306, 339),
        W("Qty", 394, 409),
        W("Unit", 420, 437),
        W("Price", 441, 462),
        W("Discount", 477, 515),
        W("Subtotal", 532, 568),
    ]
    columns = invoice._match_header(header, invoice._ITEM_COLUMNS)
    assert [c.name for c in columns] == [
        "code",
        "description",
        "options",
        "qty",
        "unit_price",
        "discount",
        "line_total",
    ]
    return columns


def test_assign_column_by_overlap():
    columns = _item_columns()
    assert invoice._assign_column(W("3", 404, 409), columns) == ("qty", False)
    assert invoice._assign_column(W("$1,200.00", 422, 462), columns) == ("unit_price", False)


def test_right_aligned_money_bleeding_left_is_not_a_straddle():
    columns = _item_columns()
    # the real sample's $3,600.00 starts 4.1 pt left of the Subtotal anchor
    name, straddles = invoice._assign_column(W("$3,600.00", 528.7, 567.8), columns)
    assert name == "line_total"
    assert straddles is False


def test_token_overrunning_column_right_edge_is_flagged():
    columns = _item_columns()
    # the real sample's merged token overruns Code's right edge by 4.1 pt
    name, straddles = invoice._assign_column(W("XY99Z000__+", 99.4, 156.5), columns)
    assert name == "code"
    assert straddles is True


# --- unit: labelled values --------------------------------------------------------


def test_label_money_ignores_tokens_left_of_label():
    # a payments row shares the visual line with the totals label
    line = [
        W("23", 24, 34),
        W("Jun", 38, 52),
        W("2026", 56, 76),
        W("$1,111.11", 100, 140),  # money LEFT of the label: not the value
        W("Sub", 420, 437),
        W("Total:", 441, 465),
        W("$3,620.00", 524, 564),
    ]
    field = invoice._find_label_money([line], [["sub", "total"]], "subtotal")
    assert field is not None and field.amount == Decimal("3620.00")


def test_abn_joins_spaced_digit_groups():
    line = [
        W("ABN", 99, 120),
        W("Number:", 124, 160),
        W("51", 170, 180),
        W("824", 184, 199),
        W("753", 203, 218),
        W("556", 222, 237),
    ]
    field = invoice._find_abn([line])
    assert field is not None and field.text == "51824753556"


# --- full pipeline on synthetic words (no OCR in the loop) ------------------------


def test_extract_invoice_synthetic_page():
    words = [
        # ID header + values
        W("Invoice", 50, 80, y=100),
        W("Date", 84, 103, y=100),
        W("Ref", 180, 197, y=100),
        W("Invoice", 310, 344, y=100),
        W("No", 348, 359, y=100),
        W("Customer", 445, 487, y=100),
        W("PO", 490, 502, y=100),
        W("No", 506, 517, y=100),
        W("23", 50, 60, y=112),
        W("Jun", 64, 79, y=112),
        W("2026", 83, 104, y=112),
        W("KOGREF99", 180, 240, y=112),
        W("77001", 310, 338, y=112),
        W("PO4512", 445, 480, y=112),
        # items table
        W("Code", 99, 121, y=240),
        W("Item", 152, 170, y=240),
        W("Qty", 394, 409, y=240),
        W("Unit", 420, 437, y=240),
        W("Price", 441, 462, y=240),
        W("Subtotal", 532, 568, y=240),
        W("AAA111", 99, 140, y=256),
        W("Widget", 152, 185, y=256),
        W("Pro", 189, 205, y=256),
        W("2", 404, 409, y=256, conf=72.0),
        W("$10.00", 430, 460, y=256),
        W("$20.00", 535, 566, y=256),
        W("Extra", 152, 180, y=267),
        W("words", 184, 210, y=267),
        W("BBB222", 99, 140, y=278),
        W("Gadget", 152, 190, y=278),
        W("3", 404, 409, y=278),
        W("$1,200.00", 422, 462, y=278),
        W("$3,600.00", 528, 568, y=278),
        # totals (right column) sharing a line with a payments row (left)
        W("$3,620.00", 100, 145, y=340),
        W("Sub", 420, 437, y=340),
        W("Total:", 441, 465, y=340),
        W("$3,620.00", 524, 564, y=340),
        W("Tax", 420, 436, y=355),
        W("(10%):", 440, 468, y=355),
        W("$329.09", 524, 558, y=355),
        W("Tax", 380, 396, y=370),
        W("Invoice", 400, 430, y=370),
        W("Total", 434, 455, y=370),
        W("(AUD):", 459, 487, y=370),
        W("$3,620.00", 524, 564, y=370),
        # ABN
        W("ABN", 99, 120, y=500),
        W("Number:", 124, 160, y=500),
        W("51824753556", 170, 240, y=500),
    ]
    result = extract_invoice(words)
    assert result.invoice_no is not None and result.invoice_no.text == "77001"
    assert result.ref is not None and result.ref.text == "KOGREF99"
    assert result.customer_po is not None and result.customer_po.text == "PO4512"
    assert result.date is not None and result.date.text == "23 Jun 2026"
    assert result.abn is not None and result.abn.text == "51824753556"
    assert result.subtotal is not None and result.subtotal.amount == Decimal("3620.00")
    assert result.tax is not None and result.tax.amount == Decimal("329.09")
    assert result.tax_rate == Decimal("0.10")
    assert result.total is not None and result.total.amount == Decimal("3620.00")
    assert len(result.items) == 2
    first, second = result.items
    assert first.qty is not None and first.qty.amount == Decimal("2")
    assert first.qty.confidence == 72.0
    assert first.unit_price is not None and first.unit_price.amount == Decimal("10.00")
    assert first.line_total is not None and first.line_total.amount == Decimal("20.00")
    assert "Widget Pro Extra words" == first.description
    assert second.qty is not None and second.qty.amount == Decimal("3")
    assert second.unit_price is not None and second.unit_price.amount == Decimal("1200.00")
    assert second.line_total is not None and second.line_total.amount == Decimal("3600.00")
    assert result.warnings == ()


def test_extract_invoice_empty_page_warns_not_raises():
    result = extract_invoice([])
    assert result.invoice_no is None and result.items == ()
    assert any("items" in w for w in result.warnings)
    assert any("total: not found" in w for w in result.warnings)


# --- end to end: generated fixture + real sample -----------------------------------


@needs_tesseract
def test_generated_invoice_fixture_end_to_end(invoice_ocr_pdf):
    with PdfDocument.open(invoice_ocr_pdf.path) as doc:
        result = doc.ocr_invoice(0)
    assert result.invoice_no is not None and result.invoice_no.text == invoice_ocr_pdf.invoice_no
    assert result.ref is not None and result.ref.text == invoice_ocr_pdf.ref
    assert result.customer_po is not None
    assert result.customer_po.text == invoice_ocr_pdf.customer_po
    assert result.date is not None and result.date.text == invoice_ocr_pdf.date
    assert result.abn is not None and result.abn.text == invoice_ocr_pdf.abn
    assert result.subtotal is not None
    assert result.subtotal.amount == Decimal(invoice_ocr_pdf.subtotal)
    assert result.tax is not None and result.tax.amount == Decimal(invoice_ocr_pdf.tax)
    assert result.tax_rate == Decimal("0.10")
    assert result.total is not None and result.total.amount == Decimal(invoice_ocr_pdf.total)
    assert len(result.items) == 2
    assert result.items[0].line_total.amount == Decimal(invoice_ocr_pdf.item1_total)
    assert result.items[1].line_total.amount == Decimal(invoice_ocr_pdf.item2_total)


@needs_tesseract
def test_real_invoice_end_to_end(real_invoice_pdf):
    with PdfDocument.open(real_invoice_pdf) as doc:
        result = doc.ocr_invoice(0)
    # By the document's own columns: INV-SAMPLE-0001 sits under "Ref" and
    # 10001 under "Invoice No" (probed geometry; the brief's "invoice number"
    # is the Ref in source-system terms — O5 decides which key the target system wants).
    assert result.ref is not None and result.ref.text == "INV-SAMPLE-0001"
    assert result.invoice_no is not None and result.invoice_no.text == "10001"
    assert result.date is None  # genuinely empty on the sample
    assert any("date: not found" in w for w in result.warnings)
    assert result.abn is not None and result.abn.text == "51824753556"
    assert result.abn.confidence >= 80
    assert result.subtotal is not None and result.subtotal.amount == Decimal("3600.00")
    assert result.tax is not None and result.tax.amount == Decimal("327.27")
    assert result.tax_rate == Decimal("0.10")
    assert result.total is not None and result.total.amount == Decimal("3600.00")
    assert len(result.items) == 1
    item = result.items[0]
    assert item.qty is not None and item.qty.amount == Decimal("3")
    assert item.unit_price is not None and item.unit_price.amount == Decimal("1200.00")
    assert item.line_total is not None and item.line_total.amount == Decimal("3600.00")
    assert "Samsung Galaxy Tab" in item.description
    assert any("XY99Z000" in f for f in item.flags)  # the column-merge, flagged
