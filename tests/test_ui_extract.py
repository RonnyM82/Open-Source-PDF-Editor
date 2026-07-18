"""Extract Text (X1 dialog/wiring + X2 whole-document OCR routing).

The routing, gate and cancellation logic is tested with fakes (no tesseract);
one end-to-end test runs real OCR on the mixed fixture under needs_tesseract.
"""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QProgressDialog, QToolBar  # noqa: E402

from pdfapp import extract_support  # noqa: E402
from pdfapp.extract_support import (  # noqa: E402
    build_extract_dialog,
    collect_sections,
    run_bulk_ocr,
)
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.ocr_cache import OcrWordCache  # noqa: E402
from pdfcore import ocr  # noqa: E402
from pdfcore.document import PdfDocument  # noqa: E402
from pdfcore.ocr import OcrWord, TesseractNotFound  # noqa: E402
from pdfcore.textsource import (  # noqa: E402
    EMPTY_AFTER_OCR,
    EMPTY_NO_OCR,
    PageText,
    format_extracted_text,
)

needs_tesseract = pytest.mark.skipif(
    not ocr.tesseract_available(), reason="tesseract binary not installed"
)


def O(text: str, x0: float = 10.0, y: float = 100.0) -> OcrWord:  # noqa: E743
    return OcrWord(text=text, bbox=(x0, y, x0 + 20.0, y + 8.0), confidence=90.0)


def test_extract_action_disabled_without_document(qapp):
    window = MainWindow()
    try:
        assert not window._extract_text_action.isEnabled()
    finally:
        window.close()


def test_extract_action_enabled_with_document_even_read_only(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        assert window.active_view.edit_mode is False  # read-only default
        assert window._extract_text_action.isEnabled()
    finally:
        window.close()


def test_extract_action_in_tools_menu_and_toolbar(qapp):
    window = MainWindow()
    try:
        menu_titles = [a.text() for a in window.menuBar().actions()]
        assert "&Tools" in menu_titles
        assert window._extract_text_action in window._tools_menu.actions()
        toolbar_actions = [
            action for tb in window.findChildren(QToolBar) for action in tb.actions()
        ]
        assert window._extract_text_action in toolbar_actions
    finally:
        window.close()


def test_collect_sections_current_page_native(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        sections, cancelled = collect_sections(doc, [0])
    assert cancelled is False
    assert len(sections) == 1
    index, page = sections[0]
    assert index == 0 and page.source == "native"
    assert "Heading for page 0" in page.text


def test_collect_sections_no_text_layer_says_so(ocr_pdf):
    """A page with no layer and no OCR must be labelled, never silently blank."""
    with PdfDocument.open(ocr_pdf.path) as doc:
        sections, _ = collect_sections(doc, [0])
    text = format_extracted_text(sections)
    assert EMPTY_NO_OCR in text
    assert text.startswith("=== Page 1 ===")


def test_format_extracted_text_headers_and_empty_variants():
    sections = [
        (0, PageText(source="native", text="hello")),
        (1, PageText(source="ocr", text="world", ocr_attempted=True)),
        (2, PageText(source="empty", text="", ocr_attempted=True)),
        (3, PageText(source="empty", text="", ocr_attempted=False)),
    ]
    out = format_extracted_text(sections)
    assert "=== Page 1 — text layer ===\nhello" in out
    assert "=== Page 2 — OCR (no text layer) ===\nworld" in out
    assert f"=== Page 3 ===\n{EMPTY_AFTER_OCR}" in out
    assert f"=== Page 4 ===\n{EMPTY_NO_OCR}" in out


def test_extract_dialog_copy_all_sets_clipboard(qapp):
    dialog = build_extract_dialog(None, "t", lambda scope: "copy me")
    dialog.copy_all()
    assert QApplication.clipboard().text() == "copy me"


def test_extract_dialog_save_to_writes_utf8(qapp, tmp_path):
    text = "café — ±3,891.00\nsecond line"
    dialog = build_extract_dialog(None, "t", lambda scope: text)
    out = tmp_path / "extract.txt"
    dialog.save_to(out)
    assert out.read_text(encoding="utf-8") == text


def test_scope_combo_drops_below_and_fits_content(theme_app):
    """Combo polish (user pass 2026-07-04): list mode (opens BELOW, items
    left-aligned) + width sized to the longest option."""
    from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox

    qapp, theme = theme_app
    theme.apply_theme(qapp)
    dialog = build_extract_dialog(None, "t", lambda scope: "x")
    combo = dialog.scope_combo
    option = QStyleOptionComboBox()
    option.initFrom(combo)
    assert combo.style().styleHint(QStyle.StyleHint.SH_ComboBox_Popup, option, combo) == 0
    assert combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToContents
    longest = combo.fontMetrics().horizontalAdvance("Whole document")
    assert combo.sizeHint().width() >= longest


def test_extract_dialog_scope_combo_reruns_runner(qapp):
    calls: list[str] = []

    def runner(scope: str) -> str:
        calls.append(scope)
        return f"text for {scope}"

    dialog = build_extract_dialog(None, "t", runner)
    assert calls == ["whole"]  # whole document is the default scope
    assert dialog.text_edit.toPlainText() == "text for whole"
    dialog.scope_combo.setCurrentIndex(1)  # Current page
    assert calls == ["whole", "current"]
    assert dialog.text_edit.toPlainText() == "text for current"


def test_extract_text_handler_builds_dialog_offscreen(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        dialog = window.extract_text()
        assert dialog is not None
        assert not dialog.isVisible()  # never exec'd offscreen
        text = dialog.text_edit.toPlainText()
        # whole document by default: all three native pages present
        assert "Heading for page 0" in text
        assert "=== Page 3 — text layer ===" in text
        assert dialog.text_edit.isReadOnly()
    finally:
        window.close()


def test_extract_is_read_only_safe(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        window.extract_text()
        assert view.undo_stack.isClean()  # nothing mutated, nothing undoable
        assert not view.undo_stack.canUndo()
    finally:
        window.close()


def test_extract_handler_without_document_is_noop(qapp):
    window = MainWindow()
    try:
        assert window.extract_text() is None
    finally:
        window.close()


# --- X2: whole document, OCR routing, gate, cancel ------------------------------


def test_collect_sections_routes_native_ocr_empty(mixed_pdf):
    fake_words = {mixed_pdf.scanned_page: [O("SCANNED")], mixed_pdf.blank_page: []}
    with PdfDocument.open(mixed_pdf.path) as doc:
        sections, cancelled = collect_sections(doc, [0, 1, 2], ocr_words_for=fake_words.get)
    assert cancelled is False
    assert [page.source for _, page in sections] == ["native", "ocr", "empty"]
    assert sections[2][1].ocr_attempted is True  # [] = OCR ran, found nothing


def test_collect_sections_cancel_midway(text_pdf):
    answers = iter([False, True])
    with PdfDocument.open(text_pdf) as doc:
        sections, cancelled = collect_sections(doc, [0, 1, 2], should_cancel=lambda: next(answers))
    assert cancelled is True
    assert len(sections) == 1  # page 0 collected, cancel fired before page 1


def test_run_bulk_ocr_fills_shared_cache(qapp, ocr_pdf, monkeypatch):
    calls: list[int] = []

    def fake_ocr(self, n, **_kwargs):
        calls.append(n)
        return [O("WORD")]

    monkeypatch.setattr(PdfDocument, "ocr_words", fake_ocr)
    cache = OcrWordCache()
    with PdfDocument.open(ocr_pdf.path) as doc:
        result = run_bulk_ocr(None, doc, [0], cache, label="test")
        assert result.cancelled is False and result.tesseract_missing is False
        assert [w.text for w in result.words_by_page[0]] == ["WORD"]
        # the SHARED cache now holds the result — no second engine call
        assert cache.words(doc, 0)[0].text == "WORD"
    assert calls == [0]


def test_run_bulk_ocr_stops_when_tesseract_missing(qapp, ocr_pdf, monkeypatch):
    def raise_missing(self, n, **_kwargs):
        raise TesseractNotFound("no binary")

    monkeypatch.setattr(PdfDocument, "ocr_words", raise_missing)
    with PdfDocument.open(ocr_pdf.path) as doc:
        result = run_bulk_ocr(None, doc, [0], OcrWordCache(), label="test")
    assert result.tesseract_missing is True
    assert result.words_by_page == {}


def test_run_bulk_ocr_cancel_returns_partial(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr(QProgressDialog, "wasCanceled", lambda self: True)
    called: list[int] = []
    monkeypatch.setattr(
        PdfDocument, "ocr_words", lambda self, n, **_k: called.append(n) or [O("W")]
    )
    with PdfDocument.open(ocr_pdf.path) as doc:
        result = run_bulk_ocr(None, doc, [0], OcrWordCache(), label="test")
    assert result.cancelled is True
    assert called == []  # cancel checked before the first page's OCR


def test_bulk_ocr_warning_gate_declined_skips_ocr(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr(extract_support, "BULK_OCR_WARN_AT", 1)
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    asked: list[int] = []

    def decline(parent, count):
        asked.append(count)
        return False

    monkeypatch.setattr(extract_support, "confirm_bulk_ocr", decline)

    def boom(self, n, **_kwargs):
        raise AssertionError("OCR must not run when the warning is declined")

    monkeypatch.setattr(PdfDocument, "ocr_words", boom)
    window = MainWindow()
    try:
        window.open_path(ocr_pdf.path)
        dialog = window.extract_text()
        assert asked == [1]
        assert EMPTY_NO_OCR in dialog.text_edit.toPlainText()  # declined ≠ silent blank
    finally:
        window.close()


def test_tesseract_missing_labels_empty_and_warns(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: False)
    window = MainWindow()
    try:
        window.open_path(ocr_pdf.path)
        dialog = window.extract_text()
        assert EMPTY_NO_OCR in dialog.text_edit.toPlainText()
        assert "Tesseract" in window.statusBar().currentMessage()
    finally:
        window.close()


def test_extract_runs_share_the_view_cache(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    calls: list[int] = []

    def fake_ocr(self, n, **_kwargs):
        calls.append(n)
        return [O("CACHED-WORD")]

    monkeypatch.setattr(PdfDocument, "ocr_words", fake_ocr)
    window = MainWindow()
    try:
        window.open_path(ocr_pdf.path)
        first = window.extract_text()
        assert "CACHED-WORD" in first.text_edit.toPlainText()
        assert "=== Page 1 — OCR (no text layer) ===" in first.text_edit.toPlainText()
        window.extract_text()
        assert calls == [0]  # second run served entirely from the view's cache
    finally:
        window.close()


def test_progress_cancel_appends_truncation_note(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    monkeypatch.setattr(QProgressDialog, "wasCanceled", lambda self: True)
    window = MainWindow()
    try:
        window.open_path(ocr_pdf.path)
        dialog = window.extract_text()
        text = dialog.text_edit.toPlainText()
        assert "[Extraction cancelled — 1 scanned page(s)" in text
        assert EMPTY_NO_OCR in text  # the skipped page is labelled, not blank
    finally:
        window.close()


@needs_tesseract
def test_extract_whole_doc_real_ocr_end_to_end(qapp, mixed_pdf):
    window = MainWindow()
    try:
        window.open_path(mixed_pdf.path)
        dialog = window.extract_text()
        text = dialog.text_edit.toPlainText()
        assert "=== Page 1 — text layer ===" in text
        assert mixed_pdf.native_marker in text
        assert "=== Page 2 — OCR (no text layer) ===" in text
        assert "SCANNED" in text
        assert f"=== Page 3 ===\n{EMPTY_AFTER_OCR}" in text  # blank raster: OCR ran, empty
    finally:
        window.close()
