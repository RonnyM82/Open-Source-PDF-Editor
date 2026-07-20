"""Offscreen tests for the Window menu (A3)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def _titles(window):
    return [a.text() for a in window._window_menu.actions()]


def test_window_menu_lists_open_documents(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.open_path(multipage_pdf)
        titles = _titles(window)
        assert text_pdf.name in titles
        assert multipage_pdf.name in titles
    finally:
        window.close()


def test_window_menu_checks_the_active_document(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.open_path(multipage_pdf)  # active
        checked = [a.text() for a in window._window_menu.actions() if a.isChecked()]
        assert checked == [multipage_pdf.name]
    finally:
        window.close()


def test_window_menu_selection_activates_tab(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)  # 3 pages
        window.open_path(multipage_pdf)  # 5 pages, active
        for action in window._window_menu.actions():
            if action.text() == text_pdf.name:
                action.trigger()
                break
        assert window.active_view.page_count == 3  # text_pdf now active
    finally:
        window.close()


def test_window_menu_empty_when_no_documents(qapp):
    window = MainWindow()
    try:
        actions = window._window_menu.actions()
        assert len(actions) == 1
        assert not actions[0].isEnabled()  # "No documents open" placeholder
    finally:
        window.close()


def test_documents_menu_is_renamed_from_window(qapp):
    """The tab-list menu reads 'Documents', not 'Window' — File → New Window now
    opens a real separate window, so a 'Window' menu that only switched tabs was
    contradictory (user feedback)."""
    window = MainWindow()
    try:
        titles = [m.title() for m in window.menuBar().findChildren(type(window._window_menu))]
        assert "&Documents" in titles
        assert "&Window" not in titles
    finally:
        window.close()


def test_new_window_action_lives_in_the_file_menu(qapp):
    window = MainWindow()
    try:
        file_menu = next(
            m for m in window.menuBar().findChildren(type(window._window_menu))
            if m.title() == "&File"
        )
        assert window._new_window_action in file_menu.actions()
        # Real window, real process: a distinct shortcut, not Ctrl+O/Ctrl+N-new-doc.
        from PySide6.QtGui import QKeySequence

        assert window._new_window_action.shortcut() == QKeySequence("Ctrl+Shift+N")
    finally:
        window.close()


def test_new_window_command_opts_out_of_single_instance(qapp):
    """The child process must NOT forward to us — it opts out so it becomes its
    own independent window."""
    window = MainWindow()
    try:
        args, env = window._new_window_command()
        assert env.get("PDF_EDITOR_NO_SINGLE_INSTANCE") == "1"
        assert args and args[0]  # a program to launch
    finally:
        window.close()


def test_new_window_spawns_a_detached_process(qapp, monkeypatch):
    """File → New Window launches a fresh process with the opt-out env."""
    import pdfapp.main_window as mw

    window = MainWindow()
    try:
        calls: list[dict] = []
        monkeypatch.setattr(
            mw.subprocess, "Popen", lambda args, **kw: calls.append({"args": args, **kw})
        )
        window.new_window()
        assert len(calls) == 1
        assert calls[0]["env"]["PDF_EDITOR_NO_SINGLE_INSTANCE"] == "1"
    finally:
        window.close()
