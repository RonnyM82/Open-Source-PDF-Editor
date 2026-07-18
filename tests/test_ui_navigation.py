"""Offscreen tests for page navigation (M3, now via the active DocumentView)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def test_navigation_moves_and_clamps(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        assert view.current_page == 0

        window.next_page()
        assert view.current_page == 1

        window.prev_page()
        assert view.current_page == 0

        window.prev_page()  # already at first -> clamp
        assert view.current_page == 0

        window.last_page()
        assert view.current_page == 4

        window.next_page()  # already at last -> clamp
        assert view.current_page == 4

        window.first_page()
        assert view.current_page == 0

        window.go_to_page(2)
        assert view.current_page == 2
        # The toolbar page indicator (spinbox + total) reflects the page.
        assert window._page_spin.value() == 3
        assert window._page_total_label.text() == "/ 5"
    finally:
        window.close()


def test_spinbox_reflects_and_drives_navigation(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        assert window._page_spin.value() == 1
        assert window._page_spin.maximum() == 5

        window.go_to_page(3)
        assert window._page_spin.value() == 4

        # Simulate the user editing the spinbox: it should drive navigation.
        window._page_spin.setValue(2)
        assert view.current_page == 1
    finally:
        window.close()


def test_nav_actions_disabled_at_bounds(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        # At first page: prev/first disabled, next/last enabled.
        assert not window._prev_action.isEnabled()
        assert not window._first_action.isEnabled()
        assert window._next_action.isEnabled()
        assert window._last_action.isEnabled()

        window.last_page()
        # At last page: next/last disabled, prev/first enabled.
        assert not window._next_action.isEnabled()
        assert not window._last_action.isEnabled()
        assert window._prev_action.isEnabled()
        assert window._first_action.isEnabled()
    finally:
        window.close()
