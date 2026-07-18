"""Windows system-font resolution for the style toolbar (pdfapp.font_files)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows font registry")

from pdfapp.font_files import resolve_font_file, system_font_map  # noqa: E402


def test_map_is_populated_with_existing_files():
    fonts = system_font_map()
    assert fonts
    sample = next(iter(fonts.values()))
    assert Path(sample).suffix.lower() in {".ttf", ".otf"}


def test_resolve_arial_regular_and_bold_differ():
    regular = resolve_font_file("Arial")
    bold = resolve_font_file("Arial", bold=True)
    assert regular and Path(regular).exists()
    assert bold and Path(bold).exists()
    assert regular != bold


def test_resolve_is_case_insensitive():
    assert resolve_font_file("ARIAL") == resolve_font_file("arial")


def test_unknown_family_returns_none():
    assert resolve_font_file("NoSuchFamilyXYZ123") is None
