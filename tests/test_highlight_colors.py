"""Pure-logic tests for the highlighter palette (pdfapp.highlight_colors). Qt-free."""

from __future__ import annotations

import pytest

from pdfapp import highlight_colors


def test_palette_is_the_six_colour_set():
    assert len(highlight_colors.HIGHLIGHTER_COLORS) == 6
    names = [name for name, _hex in highlight_colors.HIGHLIGHTER_COLORS]
    assert names == ["Yellow", "Green", "Blue", "Pink", "Orange", "Purple"]


def test_default_is_in_the_palette():
    assert highlight_colors.is_palette_hex(highlight_colors.DEFAULT_HIGHLIGHT)
    assert highlight_colors.DEFAULT_HIGHLIGHT == "#FFEB3B"  # yellow


def test_every_hex_parses_to_rgb01_in_range():
    for _name, hexstr in highlight_colors.HIGHLIGHTER_COLORS:
        rgb = highlight_colors.hex_to_rgb01(hexstr)
        assert len(rgb) == 3
        assert all(0.0 <= c <= 1.0 for c in rgb)


def test_hex_to_rgb01_known_value():
    assert highlight_colors.hex_to_rgb01("#FFEB3B") == pytest.approx((1.0, 235 / 255, 59 / 255))


def test_is_palette_hex_case_insensitive_and_strict():
    assert highlight_colors.is_palette_hex("#ffeb3b")  # case-insensitive
    assert not highlight_colors.is_palette_hex("#123456")  # not in the palette
    assert not highlight_colors.is_palette_hex(None)
    assert not highlight_colors.is_palette_hex(42)
