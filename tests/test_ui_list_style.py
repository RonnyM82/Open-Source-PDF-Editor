"""Offscreen tests for the List style control on the text-style toolbar (L3).

The sticky dropdown that decides what "Insert list…" creates — the same
InstantPopup pattern as the justification button and the highlighter swatch.
The autouse `_isolate_app_data` fixture (conftest) keeps the persisted value
out of the developer's real profile.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QToolBar, QToolButton  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402


def _blank_pdf(tmp_path):
    import pymupdf

    path = tmp_path / "blank.pdf"
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    doc.save(str(path))
    doc.close()
    return path


def test_list_style_button_shows_the_active_kind_with_the_other_in_its_menu(qapp):
    window = MainWindow()
    try:
        assert set(window._list_kind_actions) == {"bullet", "number"}
        assert window.current_list_kind() == "bullet"  # first-launch default
        assert window._list_kind_actions["bullet"].isChecked()
        assert not window._list_kind_button.icon().isNull()
        assert window._list_kind_button.toolTip()
        # One flat InstantPopup button (never a MenuButtonPopup split button —
        # Fusion draws that region as a raised pill and steals ~31px of width).
        assert window._list_kind_button.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
        menu = window._list_kind_button.menu()
        assert menu is not None
        assert [a.text() for a in menu.actions()] == ["Bulleted list", "Numbered list"]
        assert all(not a.icon().isNull() for a in menu.actions())
    finally:
        window.close()


def test_list_style_button_lives_on_the_style_toolbar_and_never_takes_focus(qapp):
    """No style control may steal the caret from an open in-place editor."""
    window = MainWindow()
    try:
        bar = window.findChild(QToolBar, "text_style_toolbar")
        assert window._list_kind_button.parent() is bar
        assert window._list_kind_button.focusPolicy() == Qt.FocusPolicy.NoFocus
    finally:
        window.close()


def test_picking_a_list_kind_makes_it_the_active_button(qapp):
    window = MainWindow()
    try:
        window._pick_list_kind("number")
        assert window.current_list_kind() == "number"
        assert window._list_kind_actions["number"].isChecked()
        assert not window._list_kind_actions["bullet"].isChecked()
        icon = window._list_kind_button.icon().pixmap(20, 20).toImage()
        assert icon == window._list_kind_actions["number"].icon().pixmap(20, 20).toImage()
    finally:
        window.close()


def test_last_used_list_kind_persists_to_the_next_window(qapp):
    w1 = MainWindow()
    try:
        w1._pick_list_kind("number")
        assert w1._settings.get("last_list_kind") == "number"
    finally:
        w1.close()
    w2 = MainWindow()
    try:
        assert w2.current_list_kind() == "number"  # the button starts there
        assert w2._list_kind_actions["number"].isChecked()
    finally:
        w2.close()


def test_corrupt_persisted_list_kind_falls_back_to_bullets(qapp):
    window = MainWindow()
    try:
        window._settings.set("last_list_kind", "roman-numerals")
        assert window._startup_list_kind() == "bullet"
    finally:
        window.close()


def test_list_style_icons_rebake_when_the_theme_changes(theme_app):
    app, theme = theme_app
    window = MainWindow()
    try:
        dark = window._list_kind_button.icon().pixmap(20, 20).toImage()
        theme.apply_theme(app, theme.LIGHT)
        assert window._list_kind_button.icon().pixmap(20, 20).toImage() != dark
    finally:
        window.close()


def test_insert_list_arms_with_the_picked_kind(qapp, tmp_path):
    window = MainWindow()
    try:
        window._pick_list_kind("number")
        window.open_path(_blank_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        window.insert_list()
        assert view._click_action == ("list", "number")
    finally:
        window.close()


def test_clicking_the_checked_insert_list_action_cancels_it(qapp, tmp_path):
    """The U4 armed-mode convention: the launching action is checkable and
    clicking it while checked disarms."""
    window = MainWindow()
    try:
        window.open_path(_blank_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        window.insert_list()
        assert view.armed_action == "list"
        assert "Esc cancels" in view._canvas._armed_chip.text()
        window.insert_list()
        assert view.armed_action is None
        assert not window._insert_list_action.isChecked()
    finally:
        window.close()
