"""The UI-side per-page OCR word cache (X0) and its invalidation funnel.

Cache behaviour is tested against a fake document (no Qt, no tesseract);
the after_command / save-as wiring is tested through a real DocumentView
offscreen with the cache seeded directly.
"""

import pytest

from pdfapp.ocr_cache import OcrWordCache

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


class FakeDoc:
    """Counts ocr_words calls; returns configured word lists."""

    def __init__(self, words_by_page):
        self._words = words_by_page
        self.calls: list[int] = []

    def ocr_words(self, n, **_kwargs):
        self.calls.append(n)
        return list(self._words.get(n, []))


def test_cache_calls_engine_once_per_page():
    doc = FakeDoc({0: ["w0"], 1: ["w1"]})
    cache = OcrWordCache()
    assert cache.words(doc, 0) == ["w0"]
    assert cache.words(doc, 0) == ["w0"]
    assert cache.words(doc, 1) == ["w1"]
    assert doc.calls == [0, 1]


def test_cache_caches_empty_results():
    """A blank page must not re-OCR on every lookup — [] is a real answer."""
    doc = FakeDoc({0: []})
    cache = OcrWordCache()
    assert cache.words(doc, 0) == []
    assert cache.words(doc, 0) == []
    assert doc.calls == [0]


def test_evict_page_refetches_only_that_page():
    doc = FakeDoc({0: ["w0"], 1: ["w1"]})
    cache = OcrWordCache()
    cache.words(doc, 0)
    cache.words(doc, 1)
    cache.evict_page(0)
    cache.words(doc, 0)
    cache.words(doc, 1)
    assert doc.calls == [0, 1, 0]


def test_clear_refetches_everything():
    doc = FakeDoc({0: ["w0"]})
    cache = OcrWordCache()
    cache.words(doc, 0)
    cache.clear()
    cache.words(doc, 0)
    assert doc.calls == [0, 0]


# --- DocumentView wiring: the after_command funnel -----------------------------


def _seeded_view(qapp, path):
    window = MainWindow()
    window.open_path(path)
    view = window.active_view
    view._ocr_words._pages = {0: [object()], 1: [object()]}
    return window, view


def test_after_command_page_scope_evicts_that_page(qapp, text_pdf):
    window, view = _seeded_view(qapp, text_pdf)
    try:
        view.after_command(("page", 0))
        assert 0 not in view._ocr_words._pages
        assert 1 in view._ocr_words._pages
    finally:
        window.close()


def test_after_command_all_scope_clears_everything(qapp, text_pdf):
    window, view = _seeded_view(qapp, text_pdf)
    try:
        view.after_command(("all", -1))
        assert view._ocr_words._pages == {}
    finally:
        window.close()


def test_save_as_clears_ocr_cache(qapp, text_pdf, tmp_path):
    window, view = _seeded_view(qapp, text_pdf)
    try:
        assert view.save_as_path(tmp_path / "copy.pdf")
        assert view._ocr_words._pages == {}
    finally:
        window.close()
