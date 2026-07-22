"""Offscreen tests for the "Show editable areas" reveal-all toggle (U5).

Per-document, edit-mode only, default ON (user decision after the hands-on
pass, 2026-07-03). Outlines every paragraph and image of the current page
from the U1 geometry cache (faint dashed, drawn under hover/selection by
drawForeground).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def _expected_count(view):
    geometry = view.page_geometry(0)
    return len(geometry.paragraphs) + len(geometry.images)


def test_default_on_outlines_appear_with_edit_mode(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        assert view.show_editable_areas is True  # default ON (decided)
        assert view._canvas._reveal_rects == []  # read-only: display gated

        view.set_edit_mode(True)
        assert len(view._canvas._reveal_rects) == _expected_count(view)
        assert window._show_areas_action.isChecked()
        assert not view._canvas.grab().isNull()  # exercises the paint path

        view.set_show_editable_areas(False)
        assert view._canvas._reveal_rects == []
        assert not window._show_areas_action.isChecked()
    finally:
        window.close()


def test_reveal_gated_by_edit_mode_flag_survives(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        assert not window._show_areas_action.isEnabled()  # read-only

        view.set_edit_mode(True)
        assert window._show_areas_action.isEnabled()
        assert len(view._canvas._reveal_rects) == _expected_count(view)

        view.set_edit_mode(False)  # leaving edit mode hides the outlines...
        assert view._canvas._reveal_rects == []
        view.set_edit_mode(True)  # ...but the flag survives the round trip
        assert len(view._canvas._reveal_rects) == _expected_count(view)
    finally:
        window.close()


def test_reveal_recomputes_after_mutation(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        before = list(view._canvas._reveal_rects)
        assert before  # on by default

        window.rotate_clockwise()  # geometry evicted; page re-shown
        after = view._canvas._reveal_rects
        assert after  # recomputed for the rotated page
        assert after != before  # rotation moved every scene rect
    finally:
        window.close()


def test_reveal_action_follows_active_tab(qapp, quote_pdf, multipage_pdf):
    """The action reflects the ACTIVE tab, and toggling one tab leaves other
    OPEN tabs untouched (per-document). New tabs seed from the persisted
    app-level default — here still ON, since nothing was toggled first."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        first = window.active_view
        first.set_edit_mode(True)
        assert window._show_areas_action.isChecked()  # default ON

        window.open_path(multipage_pdf)
        second = window.active_view
        second.set_edit_mode(True)
        assert second.show_editable_areas  # new tab seeds the default (ON)

        window._show_areas_action.setChecked(False)  # turn OFF on the active tab (2)
        assert second.show_editable_areas is False

        # The first tab is an existing open document — unchanged by the toggle.
        window._tabs.setCurrentWidget(first)
        assert first.show_editable_areas is True
        assert window._show_areas_action.isChecked()
    finally:
        window.close()


def test_new_tab_seeds_persisted_reveal_default(qapp, quote_pdf, multipage_pdf):
    """Turning reveal OFF persists it as the app-level default: the NEXT
    document opened starts with it off too (the persistence the user asked for)."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        window.active_view.set_edit_mode(True)
        window._show_areas_action.setChecked(False)  # persists show_editable_areas=False

        window.open_path(multipage_pdf)  # fresh tab seeds the persisted default
        assert window.active_view.show_editable_areas is False
    finally:
        window.close()
