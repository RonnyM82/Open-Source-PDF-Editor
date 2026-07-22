"""Offscreen tests for persisted preferences (theme / thumbnails / toggles).

The autouse `_isolate_app_data` fixture (conftest) points LOCALAPPDATA at
tmp_path, so every Settings store here — MainWindow's and app.main's — reads and
writes an isolated settings.json, never the developer's real profile.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import pdfapp.main_window as mw  # noqa: E402
from pdfapp import app as app_mod  # noqa: E402
from pdfapp import portable  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.settings import Settings  # noqa: E402


def test_theme_change_is_persisted(qapp, monkeypatch):
    window = MainWindow()
    try:
        # Don't actually restyle the shared session QApplication.
        monkeypatch.setattr(mw.theme, "apply_theme", lambda *a, **k: None)
        window._on_theme_changed(mw.theme.LIGHT)
        assert window._settings.get("theme") == "light"
    finally:
        window.close()


def test_persisted_theme_is_read_on_startup(qapp):
    Settings(portable.data_dir() / "settings.json").set("theme", "light")
    assert app_mod._persisted_theme_mode() == "light"


def test_unknown_persisted_theme_falls_back_to_dark(qapp):
    Settings(portable.data_dir() / "settings.json").set("theme", "chartreuse")
    assert app_mod._persisted_theme_mode() == "dark"


def test_thumbnails_visibility_persists_across_windows(qapp):
    w1 = MainWindow()
    try:
        w1._toggle_thumbnails(False)
        assert w1._settings.get("thumbnails_visible") is False
    finally:
        w1.close()
    w2 = MainWindow()
    try:
        assert w2._thumbs_visible is False
        assert w2._thumbs_action.isChecked() is False
    finally:
        w2.close()


def test_show_editable_areas_default_seeds_new_view(qapp, text_pdf):
    window = MainWindow()
    try:
        window._settings.set("show_editable_areas", False)
        window.open_path(text_pdf)
        assert window.active_view.show_editable_areas is False
    finally:
        window.close()


def test_dblclick_paragraph_default_seeds_new_view(qapp, text_pdf):
    window = MainWindow()
    try:
        window._settings.set("dblclick_paragraph", False)
        window.open_path(text_pdf)
        assert window.active_view.dblclick_paragraph is False
    finally:
        window.close()


def test_show_areas_toggle_persists(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window._on_show_areas_toggled(False)
        assert window._settings.get("show_editable_areas") is False
        window._on_dblclick_para_toggled(False)
        assert window._settings.get("dblclick_paragraph") is False
    finally:
        window.close()
