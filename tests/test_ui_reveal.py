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
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        first = window.active_view
        first.set_edit_mode(True)
        window._show_areas_action.setChecked(False)  # turn OFF on tab 1
        assert first.show_editable_areas is False

        window.open_path(multipage_pdf)
        second = window.active_view
        second.set_edit_mode(True)
        assert second.show_editable_areas  # its own default stays ON
        assert window._show_areas_action.isChecked()

        window._tabs.setCurrentWidget(first)
        assert not window._show_areas_action.isChecked()
    finally:
        window.close()
