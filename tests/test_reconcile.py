"""Reconciliation guard (O4): nothing unverified leaves silently.

All scenario tests build InvoiceExtracts by hand (no OCR in the loop); the
end-to-end tests run the full OCR -> extract -> reconcile pipeline on the
generated fixture and the real sample.
"""

from decimal import Decimal

import pytest

from pdfcore import ocr
from pdfcore.document import PdfDocument
from pdfcore.invoice import ExtractedField, InvoiceExtract, LineItem
from pdfcore.reconcile import abn_checksum_valid, reconcile

needs_tesseract = pytest.mark.skipif(
    not ocr.tesseract_available(), reason="tesseract binary not installed"
)

VALID_ABN = "51824753556"


def F(name: str, text: str, amount: str | None = None, conf: float = 95.0) -> ExtractedField:
    return ExtractedField(
        name=name,
        text=text,
        confidence=conf,
        bbox=(0.0, 0.0, 10.0, 10.0),
        amount=Decimal(amount) if amount is not None else None,
    )


def item(
    qty: str, unit: str, total: str, conf: float = 95.0, flags: tuple[str, ...] = ()
) -> LineItem:
    return LineItem(
        description="thing",
        qty=F("qty", qty, qty, conf=conf),
        unit_price=F("unit_price", unit, unit),
        line_total=F("line_total", total, total),
        flags=flags,
    )


def extract(**overrides) -> InvoiceExtract:
    base = dict(
        invoice_no=F("invoice_no", "77001"),
        ref=F("ref", "KOGREF99"),
        customer_po=F("customer_po", "PO4512"),
        date=F("date", "23 Jun 2026"),
        abn=F("abn", VALID_ABN),
        items=(item("2", "10.00", "20.00"), item("3", "5.00", "15.00")),
        subtotal=F("subtotal", "$35.00", "35.00"),
        tax=F("tax", "$3.50", "3.50"),
        tax_rate=Decimal("0.10"),
        total=F("total", "$38.50", "38.50"),
        warnings=(),
    )
    base.update(overrides)
    return InvoiceExtract(**base)


def check(result, name):
    return next(c for c in result.checks if c.name == name)


def test_abn_checksum():
    assert abn_checksum_valid(VALID_ABN)
    assert not abn_checksum_valid("19679514198")  # one digit off
    assert not abn_checksum_valid("1967951419")  # 10 digits
    assert not abn_checksum_valid("abn79514197")


def test_all_good_exclusive_tax():
    result = reconcile(extract())
    assert result.ok
    assert result.flags == ()
    assert result.tax_basis == "exclusive"
    assert all(c.ok for c in result.checks)


def test_all_good_gst_inclusive_tax():
    # the real sample's numbers: tax = total / 11, subtotal == total
    result = reconcile(
        extract(
            items=(item("3", "1200.00", "3600.00"),),
            subtotal=F("subtotal", "$3,600.00", "3600.00"),
            tax=F("tax", "$327.27", "327.27"),
            total=F("total", "$3,600.00", "3600.00"),
        )
    )
    assert result.ok
    assert result.tax_basis == "gst_inclusive"
    assert all(c.ok for c in result.checks)


def test_item_arithmetic_mismatch_is_flagged():
    result = reconcile(extract(items=(item("3", "10.00", "40.00"),)))
    assert not result.ok
    assert check(result, "items[0].arithmetic").ok is False
    assert any("items[0].arithmetic" in f for f in result.flags)


def test_items_sum_mismatch_is_flagged():
    result = reconcile(extract(subtotal=F("subtotal", "$99.00", "99.00")))
    assert not result.ok
    assert check(result, "items_sum").ok is False


def test_tax_matching_neither_basis_is_flagged():
    result = reconcile(extract(tax=F("tax", "$9.99", "9.99")))
    assert not result.ok
    tax_check = check(result, "tax_basis")
    assert tax_check.ok is False
    assert "neither matches" in tax_check.detail
    assert result.tax_basis is None


def test_unstated_tax_rate_cannot_verify():
    result = reconcile(extract(tax_rate=None))
    assert not result.ok
    assert check(result, "tax_basis").ok is None
    assert any("cannot verify: tax_basis" in f for f in result.flags)


def test_invalid_abn_checksum_is_flagged():
    result = reconcile(extract(abn=F("abn", "19679514198")))
    assert not result.ok
    assert check(result, "abn_checksum").ok is False


def test_missing_fields_flag_never_raise():
    result = reconcile(
        InvoiceExtract(
            invoice_no=None,
            ref=None,
            customer_po=None,
            date=None,
            abn=None,
            items=(),
            subtotal=None,
            tax=None,
            tax_rate=None,
            total=None,
            warnings=("total: not found",),
        )
    )
    assert not result.ok
    assert all(c.ok is None for c in result.checks)
    assert any(f.startswith("cannot verify:") for f in result.flags)
    assert "extraction: total: not found" in result.flags


def test_low_confidence_value_is_flagged_even_when_arithmetic_passes():
    result = reconcile(
        extract(items=(item("2", "10.00", "20.00", conf=72.0), item("3", "5.00", "15.00")))
    )
    assert check(result, "items[0].arithmetic").ok is True  # 2 x 10 = 20
    assert not result.ok
    assert any(f.startswith("low confidence: items[0].qty") for f in result.flags)


def test_item_straddle_flags_ride_along():
    flagged = item("3", "1200.00", "3600.00", flags=("column-straddle: 'XY99Z000__+'",))
    result = reconcile(
        extract(
            items=(flagged,),
            subtotal=F("subtotal", "$3,600.00", "3600.00"),
            tax=F("tax", "$327.27", "327.27"),
            total=F("total", "$3,600.00", "3600.00"),
        )
    )
    assert not result.ok
    assert any("items[0]: column-straddle" in f for f in result.flags)


# --- end to end ---------------------------------------------------------------


@needs_tesseract
def test_generated_fixture_reconciles_cleanly(invoice_ocr_pdf):
    with PdfDocument.open(invoice_ocr_pdf.path) as doc:
        result = reconcile(doc.ocr_invoice(0))
    assert all(c.ok is True for c in result.checks)  # every check evaluable + passing
    assert result.tax_basis == "gst_inclusive"  # 329.09 = 3620.00 / 11
    # no arithmetic/integrity flags; OCR confidence flags are machine-dependent
    assert not any(f.startswith(("check failed", "cannot verify")) for f in result.flags)


@needs_tesseract
def test_real_invoice_reconciles_with_human_flags(real_invoice_pdf):
    """THE demonstration: arithmetic all verifies (GST-inclusive), and the
    guard still refuses to wave the invoice through — the empty date, the
    low-confidence qty and the merged SKU token each demand a human."""
    with PdfDocument.open(real_invoice_pdf) as doc:
        result = reconcile(doc.ocr_invoice(0))
    assert all(c.ok is True for c in result.checks)
    assert result.tax_basis == "gst_inclusive"  # 327.27 = 3600.00 / 11
    assert not result.ok  # verified maths, but NOT silently pushable
    assert any("extraction: date: not found" in f for f in result.flags)
    assert any(f.startswith("low confidence: items[0].qty") for f in result.flags)
    assert any("column-straddle" in f for f in result.flags)
