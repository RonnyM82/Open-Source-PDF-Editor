"""Offscreen tests for the highlighter colour picker (A4)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp import highlight_colors  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402


def test_default_highlight_color_is_yellow(qapp):
    window = MainWindow()
    try:
        assert window._highlight_color.name().upper() == highlight_colors.DEFAULT_HIGHLIGHT
        assert window._highlight_color_actions[highlight_colors.DEFAULT_HIGHLIGHT].isChecked()
    finally:
        window.close()


def test_pick_color_updates_state_persists_and_checks(qapp):
    window = MainWindow()
    try:
        window._pick_highlight_color("#FF4081")  # pink
        assert window._highlight_color.name().upper() == "#FF4081"
        assert window._settings.get("last_highlight_color") == "#FF4081"
        assert window._highlight_color_actions["#FF4081"].isChecked()
        # Exclusive: the old yellow is now unchecked.
        assert not window._highlight_color_actions["#FFEB3B"].isChecked()
    finally:
        window.close()


def test_startup_seeds_highlight_from_settings(qapp):
    w1 = MainWindow()
    try:
        w1._pick_highlight_color("#76FF03")  # green
    finally:
        w1.close()
    w2 = MainWindow()
    try:
        assert w2._highlight_color.name().upper() == "#76FF03"
        assert w2._highlight_color_actions["#76FF03"].isChecked()
    finally:
        w2.close()


def test_invalid_persisted_color_falls_back_to_default(qapp):
    window = MainWindow()
    try:
        window._settings.set("last_highlight_color", "#000000")  # not in the palette
        assert window._startup_highlight_hex() == highlight_colors.DEFAULT_HIGHLIGHT
    finally:
        window.close()


def test_pick_color_pushes_to_open_views(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window._pick_highlight_color("#40C4FF")  # blue
        assert window.active_view._highlight_color == pytest.approx(
            highlight_colors.hex_to_rgb01("#40C4FF")
        )
    finally:
        window.close()


def test_new_view_seeds_current_highlight_color(qapp, text_pdf):
    window = MainWindow()
    try:
        window._pick_highlight_color("#B388FF")  # purple
        window.open_path(text_pdf)
        assert window.active_view._highlight_color == pytest.approx(
            highlight_colors.hex_to_rgb01("#B388FF")
        )
    finally:
        window.close()
