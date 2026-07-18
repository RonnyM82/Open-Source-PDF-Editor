"""Offscreen tests for thumbnails + render cache integration (now per-view)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def test_thumbnails_populated_on_open(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        assert window.active_view._thumbnails.count() == 5
    finally:
        window.close()


def test_clicking_thumbnail_navigates(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        view._thumbnails.setCurrentRow(3)  # selecting a thumbnail
        assert view.current_page == 3
    finally:
        window.close()


def test_navigation_syncs_thumbnail_selection(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(2)
        assert view._thumbnails.currentRow() == 2
    finally:
        window.close()


def test_open_caches_thumbnails_and_current_page(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        cache = window.active_view._cache
        # 5 thumbnails + the first main page.
        assert len(cache) == 6

        window.go_to_page(1)  # renders + caches main page 1
        assert len(cache) == 7

        # Revisiting already-cached pages does not grow the cache.
        window.go_to_page(0)
        window.go_to_page(1)
        assert len(cache) == 7
    finally:
        window.close()
