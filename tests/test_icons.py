"""Icon-set tests (restyle S4): one QtAwesome/MDI source, tooltips everywhere.

qtawesome raises at call time on an unknown glyph name — there is no silent
fallback — so the whole name table is exercised here.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QToolBar


def test_icon_factory_covers_every_key(qapp):
    from pdfapp import icons

    for key in icons._NAMES:
        assert not icons.icon(key).isNull(), key


def test_icon_factory_rejects_unknown_keys(qapp):
    from pdfapp import icons

    with pytest.raises(KeyError):
        icons.icon("no-such-key")


def test_every_toolbar_button_has_icon_and_tooltip(qapp):
    from pdfapp.main_window import MainWindow

    window = MainWindow()
    checked = 0
    for toolbar in window.findChildren(QToolBar):
        for action in toolbar.actions():
            if action.isSeparator() or not action.text():
                continue  # widget slots (page spin, font combo, swatch, label)
            assert not action.icon().isNull(), action.text()
            assert action.toolTip(), action.text()
            checked += 1
    assert checked >= 15  # both toolbars are populated
    window.close()


def test_icons_rebake_when_theme_changes(theme_app):
    from pdfapp.main_window import MainWindow

    app, theme = theme_app
    window = MainWindow()
    dark_img = window._save_action.icon().pixmap(20, 20).toImage()
    theme.apply_theme(app, theme.LIGHT)
    light_img = window._save_action.icon().pixmap(20, 20).toImage()
    assert dark_img != light_img  # glyph colour follows the mode
    window.close()


def test_style_controls_keep_nofocus_with_icons(qapp):
    from pdfapp.main_window import MainWindow

    window = MainWindow()
    bar = window.findChild(QToolBar, "text_style_toolbar")
    for action in (
        window._bold_action,
        window._italic_action,
        window._underline_action,
        window._strike_action,
        window._super_action,
        window._sub_action,
    ):
        button = bar.widgetForAction(action)
        assert button.focusPolicy() == Qt.FocusPolicy.NoFocus
    for widget in (window._font_combo, window._color_button):
        assert widget.focusPolicy() == Qt.FocusPolicy.NoFocus
    # The size spin deliberately takes ClickFocus so sizes can be TYPED
    # (user request; editingFinished refocuses the open editor).
    assert window._size_spin.focusPolicy() == Qt.FocusPolicy.ClickFocus
    window.close()
