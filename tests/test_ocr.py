"""Engine OCR (O1): word boxes from no-text-layer pages via Tesseract.

Tests that RUN tesseract skip when the binary is absent; the unavailability
path is tested unconditionally with a monkeypatched resolver — the real
failure path, not a return-value check. Boxes come back in unrotated page
points; the rotated-page test asserts that against an oracle built into the
fixture (independent of the engine's derotation).
"""

import pymupdf
import pytest

from pdfcore import ocr
from pdfcore.document import PdfDocument

needs_tesseract = pytest.mark.skipif(
    not ocr.tesseract_available(), reason="tesseract binary not installed"
)


def test_frozen_resolver_uses_bundled_copy_only(monkeypatch, tmp_path):
    """In a frozen build the resolver must return the bundled path — never
    fall back to a system install (a broken bundle has to fail loudly)."""
    monkeypatch.setattr(ocr.sys, "frozen", False, raising=False)
    dev_path = ocr.tesseract_command()  # dev resolution, for contrast

    monkeypatch.setattr(ocr.sys, "frozen", True, raising=False)
    monkeypatch.setattr(ocr.sys, "_MEIPASS", str(tmp_path), raising=False)
    frozen_path = ocr.tesseract_command()
    assert frozen_path == str(tmp_path / "tesseract" / "tesseract.exe")
    # No bundled copy exists at that path, so frozen mode reports unavailable
    # even though this machine has a working system install (dev_path).
    assert not ocr.tesseract_available()
    assert frozen_path != dev_path


def test_missing_tesseract_raises_before_any_work(monkeypatch, ocr_pdf):
    monkeypatch.setattr(ocr, "tesseract_command", lambda: r"Z:\nowhere\tesseract.exe")
    with PdfDocument.open(ocr_pdf.path) as doc:
        with pytest.raises(ocr.TesseractNotFound, match="not found"):
            doc.ocr_words(0)


@needs_tesseract
def test_reads_words_from_no_text_layer_page(ocr_pdf):
    with pymupdf.open(ocr_pdf.path) as raw:
        assert raw[0].get_text().strip() == ""  # fixture integrity: OCR is the only way
    with PdfDocument.open(ocr_pdf.path) as doc:
        words = doc.ocr_words(0)
        page_w, page_h = doc.page_size(0)
    texts = [w.text for w in words]
    for expected in ocr_pdf.expected_words:
        assert expected in texts
    for word in words:
        x0, y0, x1, y1 = word.bbox
        assert 0.0 <= x0 < x1 <= page_w + 0.5
        assert 0.0 <= y0 < y1 <= page_h + 0.5
        assert 0.0 <= word.confidence <= 100.0


@needs_tesseract
def test_word_box_matches_known_layout(ocr_pdf):
    with PdfDocument.open(ocr_pdf.path) as doc:
        words = doc.ocr_words(0)
    anchor = next(w for w in words if w.text == ocr_pdf.anchor_word)
    origin_x, baseline_y = ocr_pdf.anchor_origin
    size = ocr_pdf.fontsize
    x0, y0, x1, y1 = anchor.bbox
    assert abs(x0 - origin_x) <= 5.0  # left edge at the insert_text x
    # Ink top sits between one em above the baseline and the x-height region.
    assert baseline_y - size <= y0 <= baseline_y - 0.4 * size
    # "Invoice" has no descenders: ink bottom is at/near the baseline.
    assert baseline_y - 2.0 <= y1 <= baseline_y + 0.35 * size
    assert anchor.confidence >= 60.0


@needs_tesseract
def test_boxes_are_dpi_invariant(ocr_pdf):
    """Boxes are in PAGE POINTS: rendering DPI must not move them."""
    with PdfDocument.open(ocr_pdf.path) as doc:
        at_300 = {w.text: w.bbox for w in doc.ocr_words(0)}
        at_150 = {w.text: w.bbox for w in doc.ocr_words(0, dpi=150)}
    for text in ocr_pdf.expected_words:
        assert text in at_300 and text in at_150
        for a, b in zip(at_300[text], at_150[text], strict=True):
            assert abs(a - b) <= 4.0


@needs_tesseract
def test_hostile_tessdata_prefix_cannot_hijack(monkeypatch, ocr_pdf):
    """A user-global TESSDATA_PREFIX overrides tesseract's exe-relative model
    lookup (frozen-probe verified) — extract_words must pin it to the exe's
    own tessdata for the call, and restore the environment afterwards."""
    monkeypatch.setenv("TESSDATA_PREFIX", r"Z:\definitely\not\real")
    with PdfDocument.open(ocr_pdf.path) as doc:
        words = doc.ocr_words(0)
    texts = [w.text for w in words]
    for expected in ocr_pdf.expected_words:
        assert expected in texts
    import os

    assert os.environ["TESSDATA_PREFIX"] == r"Z:\definitely\not\real"  # restored


@needs_tesseract
def test_blank_page_gives_no_words(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "blank.pdf"
    doc.save(str(path))
    doc.close()
    with pymupdf.open(path) as raw:
        assert ocr.extract_words(raw, 0) == []


@needs_tesseract
def test_rotated_page_boxes_map_to_unrotated_space(ocr_rotated_pdf):
    with PdfDocument.open(ocr_rotated_pdf.path) as doc:
        assert doc.page_rotation(0) == 90
        words = doc.ocr_words(0)
    match = next(w for w in words if w.text == ocr_rotated_pdf.word)
    x0, y0, x1, y1 = match.bbox
    assert x1 - x0 < y1 - y0  # the word runs vertically in unrotated space
    for got, exp in zip(match.bbox, ocr_rotated_pdf.expected_bbox, strict=True):
        assert abs(got - exp) <= 8.0


@needs_tesseract
def test_real_invoice_reads_business_fields(real_invoice_pdf):
    """The fields the OCR feature exists for, on the real sample (skips if absent)."""
    with PdfDocument.open(real_invoice_pdf) as doc:
        words = doc.ocr_words(0)
        page_w, page_h = doc.page_size(0)
    texts = [w.text for w in words]
    assert "INV-SAMPLE-0001" in texts  # invoice number, exact
    assert "51824753556" in texts  # ABN, exact (mod-89 valid)
    assert "$1,200.00" in texts and "$327.27" in texts
    assert texts.count("$3,600.00") >= 4  # subtotal/total/paid/payment rows
    # Known defect, documented for O3: the second SKU-column token straddles
    # into the description column ("XY99Z000" + "+"); column-aware parsing
    # must split or flag it. Assert only the stable prefix.
    assert any(t.startswith("XY99Z000") for t in texts)
    abn = next(w for w in words if w.text == "51824753556")
    assert abn.confidence >= 80.0
    for word in words:
        x0, y0, x1, y1 = word.bbox
        assert 0.0 <= x0 < x1 <= page_w + 0.5
        assert 0.0 <= y0 < y1 <= page_h + 0.5
