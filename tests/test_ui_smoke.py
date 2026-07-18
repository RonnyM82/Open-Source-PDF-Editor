"""Offscreen UI smoke test.

Verifies the open -> render -> display pipeline and the qt_image seam without a
visible window. Uses only PySide6 (already a dependency) with the "offscreen"
platform plugin — no GUI test framework is added. The shared ``qapp`` fixture
lives in conftest.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage  # noqa: E402

from pdfapp import app as app_module  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.qt_image import rendered_page_to_qimage  # noqa: E402
from pdfcore import ocr  # noqa: E402
from pdfcore.document import PdfDocument  # noqa: E402

needs_tesseract = pytest.mark.skipif(
    not ocr.tesseract_available(), reason="tesseract binary not installed"
)


def test_open_renders_first_page(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        assert view is not None
        assert view._canvas.has_page
        assert view.page_count == 3
        assert view.current_page == 0
    finally:
        window.close()


def test_qt_image_seam_preserves_dimensions_and_rgb_format(qapp, text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        rp = doc.render_page(0, zoom=1.5)
        img = rendered_page_to_qimage(rp)
    assert img.width() == rp.width
    assert img.height() == rp.height
    assert img.format() == QImage.Format.Format_RGB888


def test_qt_image_seam_alpha_format(qapp, text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        rp = doc.render_page(0, alpha=True)
        img = rendered_page_to_qimage(rp)
    assert img.format() == QImage.Format.Format_RGBA8888


def test_qt_image_seam_grayscale_format(qapp, text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        rp = doc.render_page_at_dpi(0, 72, gray=True)
        img = rendered_page_to_qimage(rp)
    assert rp.channels == 1
    assert img.format() == QImage.Format.Format_Grayscale8


# --- OCR smoke branch (O2) ---------------------------------------------------
# The frozen-build check for the bundled tesseract runtime. Success asserts on
# the WORD LIST, never exit codes (tesseract exits 0 even when a missing
# config/tessdata file means it produced no output — spike-verified).


@needs_tesseract
def test_ocr_smoke_succeeds_and_writes_result(monkeypatch, tmp_path, ocr_pdf):
    out = tmp_path / "smoke_result.txt"
    monkeypatch.setenv("PDF_EDITOR_SMOKE_OUT", str(out))
    assert app_module._run_ocr_smoke(str(ocr_pdf.path)) == 0
    assert out.read_text(encoding="utf-8").startswith("OCR OK")


@needs_tesseract
def test_ocr_smoke_fails_on_wordless_page(monkeypatch, tmp_path):
    import pymupdf

    blank = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(blank))
    doc.close()
    monkeypatch.delenv("PDF_EDITOR_SMOKE_OUT", raising=False)
    assert app_module._run_ocr_smoke(str(blank)) == 1


def test_ocr_smoke_fails_loudly_without_tesseract(monkeypatch, tmp_path, ocr_pdf):
    """The bundled-copy-missing case: exactly what a broken bundle looks like."""
    out = tmp_path / "smoke_result.txt"
    monkeypatch.setenv("PDF_EDITOR_SMOKE_OUT", str(out))
    monkeypatch.setattr(ocr, "tesseract_command", lambda: r"Z:\nowhere\tesseract.exe")
    assert app_module._run_ocr_smoke(str(ocr_pdf.path)) == 1
    assert out.read_text(encoding="utf-8").startswith("OCR FAIL")
