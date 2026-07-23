"""Offscreen tests for the U7 "Editing gestures" cheat-sheet dialog."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.help_dialog import GestureHelpDialog, gestures_html  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402


def test_cheat_sheet_documents_every_surface():
    html = gestures_html()
    for phrase in (
        "Edit mode (Ctrl+E)",
        "Markup mode",
        "Show editable areas",
        "Double-click edits paragraph",
        "Ctrl+double-click",
        "Ctrl+drag",
        "Delete",
        "Esc",
        "Highlight",
        "Ctrl+Shift+H",
        "Ctrl+Z",
        "Scroll at a page edge",
    ):
        assert phrase in html, phrase


def test_dialog_builds_offscreen(qapp):
    dialog = GestureHelpDialog()
    assert dialog.windowTitle() == "Editing gestures"
    dialog.deleteLater()


def test_help_menu_action_returns_dialog_without_exec(qapp):
    window = MainWindow()
    try:
        dialog = window.show_gesture_help()  # offscreen: no modal exec
        assert dialog.windowTitle() == "Editing gestures"
        assert window._gestures_action.isEnabled()  # never gated
    finally:
        window.close()


def test_show_diagnostics_log_action_reveals_the_log(qapp, monkeypatch):
    from pdfapp import diagnostics

    window = MainWindow()
    try:
        calls = []
        monkeypatch.setattr(diagnostics, "reveal_log", lambda: (calls.append(1), True)[1])
        assert window._show_log_action.isEnabled()  # never gated
        window._show_log_action.trigger()  # the Help menu item
        assert calls == [1]  # routed to diagnostics.reveal_log
    finally:
        window.close()


def test_show_diagnostics_log_handles_missing_log_offscreen(qapp, monkeypatch):
    from pdfapp import diagnostics

    window = MainWindow()
    try:
        monkeypatch.setattr(diagnostics, "reveal_log", lambda: False)
        # Not visible -> no modal info box to hang the test; returns False.
        assert window.show_diagnostics_log() is False
    finally:
        window.close()
