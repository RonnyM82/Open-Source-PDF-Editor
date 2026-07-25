"""Offscreen tests for the Help → About dialog."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp import __version__ as APP_VERSION  # noqa: E402
from pdfapp.about_dialog import (  # noqa: E402
    APP_NAME,
    AboutDialog,
    about_html,
    component_versions,
)
from pdfapp.main_window import MainWindow  # noqa: E402


def test_component_versions_carry_app_and_stack():
    versions = component_versions()
    assert versions[APP_NAME] == APP_VERSION
    # The live stack — module attributes, so these are real, never "—".
    assert versions["PySide6"] != "—"
    assert versions["PyMuPDF"] != "—"
    assert "Python" in versions


def test_about_html_states_version_and_licence():
    html = about_html()
    assert APP_NAME in html
    assert APP_VERSION in html
    assert "AGPL-3.0" in html
    assert "PyMuPDF" in html
    assert "github.com" in html  # source-availability link


def test_dialog_builds_offscreen(qapp):
    dialog = AboutDialog()
    assert dialog.windowTitle() == f"About {APP_NAME}"
    dialog.deleteLater()


def test_help_menu_action_returns_dialog_without_exec(qapp):
    window = MainWindow()
    try:
        dialog = window.show_about()  # offscreen: no modal exec
        assert dialog.windowTitle() == f"About {APP_NAME}"
        assert window._about_action.isEnabled()  # never gated
    finally:
        window.close()
