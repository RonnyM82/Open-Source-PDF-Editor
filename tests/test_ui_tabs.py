"""Offscreen tests for multi-document tabs (A2)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def test_open_two_files_creates_two_tabs(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.open_path(multipage_pdf)
        assert window._tabs.count() == 2
        # The most-recently-opened document is active.
        assert window.active_view.page_count == 5
    finally:
        window.close()


def test_switching_tabs_changes_active_document(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)  # 3 pages
        window.open_path(multipage_pdf)  # 5 pages, active
        window._tabs.setCurrentIndex(0)
        assert window.active_view.page_count == 3
        assert window._page_total_label.text() == "/ 3"
        window._tabs.setCurrentIndex(1)
        assert window.active_view.page_count == 5
        assert window._page_total_label.text() == "/ 5"
    finally:
        window.close()


def test_per_document_state_is_independent(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view0 = window.active_view
        window.open_path(multipage_pdf)
        view1 = window.active_view

        window.go_to_page(3)  # navigate in the active (view1)
        assert view1.current_page == 3

        window._tabs.setCurrentIndex(0)
        assert window.active_view is view0
        assert view0.current_page == 0  # view0 kept its own page

        window.rotate_clockwise()  # mutate view0 only
        assert view0.dirty is True
        assert view1.dirty is False
    finally:
        window.close()


def test_opening_same_path_focuses_existing_tab(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.open_path(text_pdf)  # same path -> no duplicate
        assert window._tabs.count() == 1
    finally:
        window.close()


def test_tab_title_shows_dirty_marker(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        index = window._tabs.currentIndex()
        assert not window._tabs.tabText(index).endswith("*")
        window.rotate_clockwise()
        assert window._tabs.tabText(index).endswith("*")
    finally:
        window.close()


def test_empty_state_when_no_documents(qapp):
    window = MainWindow()
    try:
        assert window.active_view is None
        assert window._tabs.count() == 0
        assert not window._next_action.isEnabled()
        assert not window._save_action.isEnabled()
        assert not window._rotate_cw_action.isEnabled()
    finally:
        window.close()
