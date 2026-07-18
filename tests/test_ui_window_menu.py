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
