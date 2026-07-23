"""Offscreen tests for the Annotate toolbar (A5)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QToolBar  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402


def _annotate_bar(window):
    return next(tb for tb in window.findChildren(QToolBar) if tb.objectName() == "annotate_toolbar")


def test_annotate_toolbar_exists_with_actions_and_swatch(qapp):
    window = MainWindow()
    try:
        bar = _annotate_bar(window)
        actions = bar.actions()
        assert window._highlight_action in actions
        assert window._insert_comment_action in actions
        assert window._insert_callout_action in actions
        # The colour swatch button is on the bar.
        assert bar.widgetForAction(bar.actions()[1]) is window._highlight_color_button
    finally:
        window.close()


def test_annotate_actions_enabled_in_markup_mode(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        assert window.active_view.edit_mode is False  # markup, not edit
        assert window._highlight_action.isEnabled()
        assert window._insert_comment_action.isEnabled()
        assert window._insert_callout_action.isEnabled()
    finally:
        window.close()


def test_annotate_action_icons_are_themed(qapp):
    window = MainWindow()
    try:
        for action in window._annotate_actions:
            assert not action.icon().isNull()  # baked via _icon_keys/_assign_icons
    finally:
        window.close()


def test_swatch_menu_lists_the_palette(qapp):
    window = MainWindow()
    try:
        menu = window._highlight_color_button.menu()
        labels = [a.text() for a in menu.actions()]
        assert labels == ["Yellow", "Green", "Blue", "Pink", "Orange", "Purple"]
    finally:
        window.close()


def test_swatch_repaints_on_colour_pick(qapp):
    window = MainWindow()
    try:
        before = window._highlight_color_button.icon().cacheKey()
        window._pick_highlight_color("#B388FF")  # purple
        after = window._highlight_color_button.icon().cacheKey()
        assert before != after  # the swatch was repainted
    finally:
        window.close()


def test_swatch_menu_shares_actions_with_the_annotate_submenu(qapp):
    """Picking from the toolbar swatch checks the same action the menu shows —
    they are the same QAction objects, so state stays in sync."""
    window = MainWindow()
    try:
        window._pick_highlight_color("#40C4FF")  # blue
        menu_action = window._highlight_color_actions["#40C4FF"]
        swatch_actions = window._highlight_color_button.menu().actions()
        assert menu_action in swatch_actions
        assert menu_action.isChecked()
    finally:
        window.close()


def test_annotate_toolbar_object_name_for_saved_layout(qapp):
    window = MainWindow()
    try:
        assert _annotate_bar(window).objectName() == "annotate_toolbar"
    finally:
        window.close()
