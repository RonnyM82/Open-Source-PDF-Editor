"""Search UI (SR2): the per-tab bar, whole-document navigation, highlights.

Search is ALWAYS case-insensitive (by design — no toggle exists; the engine
tests pin the matching itself). Tests call run_search() directly to bypass
the typing debounce; Enter is tested as the immediate-run path.
"""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QProgressDialog  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfcore import ocr  # noqa: E402
from pdfcore.document import PdfDocument  # noqa: E402
from pdfcore.ocr import OcrWord  # noqa: E402

needs_tesseract = pytest.mark.skipif(
    not ocr.tesseract_available(), reason="tesseract binary not installed"
)


def O(text: str, x0: float = 100.0, y: float = 100.0) -> OcrWord:  # noqa: E743
    return OcrWord(text=text, bbox=(x0, y, x0 + 60.0, y + 12.0), confidence=90.0)


def _open(qapp, path):
    window = MainWindow()
    window.open_path(path)
    return window, window.active_view


def _set_query(view, text: str) -> None:
    view._search_bar._query.setText(text)
    view._search_timer.stop()  # tests drive run_search explicitly


def _key(widget, key, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def test_find_action_enabled_only_with_document(qapp, text_pdf):
    window = MainWindow()
    try:
        assert not window._find_action.isEnabled()
        window.open_path(text_pdf)
        assert window._find_action.isEnabled()
        assert window.active_view.edit_mode is False  # read feature: no gate
    finally:
        window.close()


def test_find_action_has_ctrl_f(qapp):
    window = MainWindow()
    try:
        shortcuts = [s.toString() for s in window._find_action.shortcuts()]
        assert "Ctrl+F" in shortcuts
    finally:
        window.close()


def test_find_opens_bar_and_focuses_query(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        assert view._search_bar.isHidden()
        window.find_in_document()
        assert not view._search_bar.isHidden()
        # No active window offscreen, so hasFocus() can't be used —
        # focusWidget() records where setFocus landed even unshown.
        assert view._search_bar.focusWidget() is view._search_bar._query
    finally:
        window.close()


def test_count_label_and_current_page_follow_hits(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        view.open_search()
        _set_query(view, "Lorem")
        view.run_search()
        assert view._search_bar.status() == "1 of 60"
        assert view.current_page == 0
    finally:
        window.close()


def test_no_matches_label(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        view.open_search()
        _set_query(view, "zzz-not-here")
        view.run_search()
        assert view._search_bar.status() == "No matches"
        assert view._canvas._search_rects == [] and view._canvas._search_current == []
    finally:
        window.close()


def test_next_prev_wrap_across_pages(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        view.open_search()
        _set_query(view, "Heading")  # one hit per page: 3 total
        view.run_search()
        assert view._search_bar.status() == "1 of 3" and view.current_page == 0
        view.next_match()
        assert view._search_bar.status() == "2 of 3" and view.current_page == 1
        view.next_match()
        view.next_match()  # wraps back to the first hit
        assert view._search_bar.status() == "1 of 3" and view.current_page == 0
        view.prev_match()  # wraps backwards to the last hit
        assert view._search_bar.status() == "3 of 3" and view.current_page == 2
    finally:
        window.close()


def test_enter_and_shift_enter_navigate(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        view.open_search()
        _set_query(view, "Heading")
        _key(view._search_bar, Qt.Key.Key_Return)  # immediate run -> first hit
        assert view._search_bar.status() == "1 of 3"
        _key(view._search_bar, Qt.Key.Key_Return)
        assert view._search_bar.status() == "2 of 3"
        _key(view._search_bar, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        assert view._search_bar.status() == "1 of 3"
    finally:
        window.close()


def test_highlights_reach_canvas_current_page_only(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        view.open_search()
        _set_query(view, "Heading")
        view.run_search()
        # 3 hits, one per page: the shown page carries ONE current rect and
        # no other rects (the other hits live on other pages).
        assert len(view._canvas._search_current) == 1
        assert view._canvas._search_rects == []
        view.next_match()  # page 1 shows its own hit as current
        assert view.current_page == 1
        assert len(view._canvas._search_current) == 1
    finally:
        window.close()


def test_esc_in_bar_closes_and_clears(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        view.open_search()
        _set_query(view, "Heading")
        view.run_search()
        _key(view._search_bar, Qt.Key.Key_Escape)
        assert view._search_bar.isHidden()
        assert view._search_hits == []
        assert view._canvas._search_current == []
    finally:
        window.close()


def test_canvas_esc_priority_armed_then_selection_then_search(qapp, quote_pdf):
    window, view = _open(qapp, quote_pdf.path)
    try:
        view.set_edit_mode(True)
        view.open_search()
        view._canvas.arm_insert_point("chip")
        view._selection = ("text", 0, object())
        view._canvas.set_selection("text", (0.0, 0.0, 10.0, 10.0))

        view._on_escape()  # 1) armed mode cancelled first
        assert not view._canvas.is_armed
        assert view._selection is not None
        assert not view._search_bar.isHidden()

        view._on_escape()  # 2) selection cleared second
        assert view._selection is None
        assert not view._search_bar.isHidden()

        view._on_escape()  # 3) search closes LAST
        assert view._search_bar.isHidden()
    finally:
        window.close()


def test_mutation_clears_search_results(qapp, multipage_pdf):
    window, view = _open(qapp, multipage_pdf)
    try:
        view.set_edit_mode(True)
        view.open_search()
        _set_query(view, "PAGE-MARKER")
        view.run_search()
        assert view._search_bar.status() == "1 of 5"
        window.rotate_clockwise()  # any mutation -> stale hits must drop
        assert view._search_hits == []
        assert view._search_bar.status() == ""
        assert view._canvas._search_current == []
        assert not view._search_bar.isHidden()  # the bar itself survives
    finally:
        window.close()


def test_search_is_read_only_safe(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        assert view.edit_mode is False
        view.open_search()
        _set_query(view, "Lorem")
        view.run_search()
        view.next_match()
        assert view.undo_stack.isClean()
        assert not view.undo_stack.canUndo()
    finally:
        window.close()


def test_per_tab_state_is_isolated(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        first = window.active_view
        first.open_search()
        _set_query(first, "Heading")
        first.run_search()

        window.open_path(multipage_pdf)
        second = window.active_view
        assert second is not first
        second.open_search()
        _set_query(second, "PAGE-MARKER-004")
        second.run_search()

        assert first._search_bar.query() == "Heading"
        assert first._search_bar.status() == "1 of 3"
        assert second._search_bar.status() == "1 of 1"
        assert second.current_page == 4

        window._tabs.setCurrentWidget(first)  # Ctrl+F targets the active tab
        window.find_in_document()
        assert not first._search_bar.isHidden()
    finally:
        window.close()


def test_search_query_shorter_lifecycle_clear_on_empty(qapp, text_pdf):
    window, view = _open(qapp, text_pdf)
    try:
        view.open_search()
        _set_query(view, "Heading")
        view.run_search()
        assert view._search_hits
        _set_query(view, "   ")
        view.run_search()
        assert view._search_hits == []
        assert view._search_bar.status() == ""
    finally:
        window.close()


# --- SR4: the OCR fallback (offer -> shared cache -> the same matcher) ----------


def test_offer_visible_for_scanned_pages_without_auto_ocr(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)

    def boom(self, n, **_kwargs):
        raise AssertionError("OCR must be user-initiated, never automatic")

    monkeypatch.setattr(PdfDocument, "ocr_words", boom)
    window, view = _open(qapp, ocr_pdf.path)
    try:
        view.open_search()
        _set_query(view, "sample")
        view.run_search()
        assert view._search_bar.ocr_offer_visible()
        assert view._search_bar.status() == "No matches"  # native search only
    finally:
        window.close()


def test_offer_hidden_when_tesseract_missing_with_message(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: False)
    window, view = _open(qapp, ocr_pdf.path)
    try:
        view.open_search()
        _set_query(view, "sample")
        view.run_search()
        assert not view._search_bar.ocr_offer_visible()
        assert (
            view._search_bar.status()
            == "No text layer — OCR is unavailable (Tesseract not installed)."
        )
    finally:
        window.close()


def test_accept_offer_searches_ocr_words_case_insensitively(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    monkeypatch.setattr(
        PdfDocument,
        "ocr_words",
        lambda self, n, **_k: [O("Invoice"), O("INV-SAMPLE-0001", x0=200.0)],
    )
    window, view = _open(qapp, ocr_pdf.path)
    try:
        view.open_search()
        _set_query(view, "sample")  # lower-case query, upper-case OCR word
        view.search_with_ocr()
        assert view._search_ocr_opted_in
        assert view._search_bar.status() == "1 of 1"
        assert len(view._canvas._search_current) == 1
        assert not view._search_bar.ocr_offer_visible()  # opted in: offer gone
    finally:
        window.close()


def test_opted_in_reuses_cache_across_queries(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    calls: list[int] = []

    def fake_ocr(self, n, **_kwargs):
        calls.append(n)
        return [O("Invoice"), O("INV-SAMPLE-0001", x0=200.0)]

    monkeypatch.setattr(PdfDocument, "ocr_words", fake_ocr)
    window, view = _open(qapp, ocr_pdf.path)
    try:
        view.open_search()
        _set_query(view, "sample")
        view.search_with_ocr()
        _set_query(view, "invoice")
        view.run_search()  # still opted in — served from the shared cache
        assert view._search_bar.status() == "1 of 1"
        assert calls == [0]
    finally:
        window.close()


def test_mutation_evicts_page_then_requery_reocrs(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    calls: list[int] = []

    def fake_ocr(self, n, **_kwargs):
        calls.append(n)
        return [O("INV-SAMPLE-0001")]

    monkeypatch.setattr(PdfDocument, "ocr_words", fake_ocr)
    window, view = _open(qapp, ocr_pdf.path)
    try:
        view.open_search()
        _set_query(view, "sample")
        view.search_with_ocr()
        assert calls == [0]
        view.after_command(("page", 0))  # mutation: hits cleared, page evicted
        assert view._search_hits == []
        view.run_search()  # opted in: the evicted page re-OCRs
        assert calls == [0, 0]
        assert view._search_bar.status() == "1 of 1"
    finally:
        window.close()


def test_cancel_mid_ocr_drops_opt_in_and_keeps_offer(qapp, ocr_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    monkeypatch.setattr(QProgressDialog, "wasCanceled", lambda self: True)
    monkeypatch.setattr(PdfDocument, "ocr_words", lambda self, n, **_k: [O("W")])
    window, view = _open(qapp, ocr_pdf.path)
    try:
        view.open_search()
        _set_query(view, "sample")
        view.search_with_ocr()
        assert not view._search_ocr_opted_in  # partial coverage never poses as complete
        assert view._search_bar.ocr_offer_visible()
    finally:
        window.close()


def test_unsearchable_document_message(qapp, unsearchable_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)
    monkeypatch.setattr(PdfDocument, "ocr_words", lambda self, n, **_k: [])
    window, view = _open(qapp, unsearchable_pdf)
    try:
        view.open_search()
        _set_query(view, "anything")
        view.search_with_ocr()
        assert view._search_bar.status() == "This document isn't searchable."
    finally:
        window.close()


def test_mixed_doc_native_hits_come_before_ocr_hits(qapp, mixed_pdf, monkeypatch):
    monkeypatch.setattr("pdfcore.ocr.tesseract_available", lambda: True)

    def fake_ocr(self, n, **_kwargs):
        return [O("MARKER")] if n == mixed_pdf.scanned_page else []

    monkeypatch.setattr(PdfDocument, "ocr_words", fake_ocr)
    window, view = _open(qapp, mixed_pdf.path)
    try:
        view.open_search()
        _set_query(view, "marker")  # native MIXED-NATIVE-MARKER + OCR MARKER
        view.search_with_ocr()
        assert view._search_bar.status() == "1 of 2"
        assert [h.page_index for h in view._search_hits] == [0, 1]
    finally:
        window.close()


@needs_tesseract
def test_search_ocr_end_to_end(qapp, ocr_pdf):
    """Real OCR through the real cache: lower-case query finds the fixture's
    upper-case word on a page with no text layer."""
    window, view = _open(qapp, ocr_pdf.path)
    try:
        view.open_search()
        _set_query(view, "sample")
        view.run_search()
        assert view._search_bar.status() == "No matches"  # native only, so far
        view.search_with_ocr()
        assert view._search_bar.status() == "1 of 1"
        assert view._search_hits[0].page_index == 0
        assert len(view._canvas._search_current) == 1
    finally:
        window.close()


def test_search_bar_keeps_its_natural_height(qapp, quote_pdf):
    """The bar must never grab layout stretch (user screenshot, 2026-07-18:
    giant search bar, half-size page). A HORIZONTAL QSplitter is vertically
    Preferred — same as the bar — so without explicit stretch factors the
    view split its height 50/50 whenever the bar opened. Latent since SR2."""
    from pdfapp.main_window import MainWindow

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        window.resize(1400, 1000)
        window.show()
        view = window.active_view
        view.open_search()
        qapp.processEvents()
        bar = view._search_bar
        assert bar.height() <= bar.sizeHint().height() + 8
        assert bar.height() < view.height() / 4  # the splitter keeps the rest
    finally:
        window.close()
