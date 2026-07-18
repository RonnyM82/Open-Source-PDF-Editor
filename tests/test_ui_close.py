"""Offscreen tests for tab close handling (A4).

The dirty-prompt path is exercised by monkeypatching QMessageBox.question (the
window is never shown, so _close_tab's prompt would otherwise be skipped only in
closeEvent, which guards on isVisible()).
"""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402


def test_closing_clean_tab_removes_it(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.open_path(multipage_pdf)
        assert window._tabs.count() == 2
        window._close_tab(0)  # text_pdf is clean -> no prompt
        assert window._tabs.count() == 1
        assert window.active_view.page_count == 5
    finally:
        window.close()


def test_closing_last_tab_returns_to_empty_state(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window._close_tab(0)
        assert window._tabs.count() == 0
        assert window.active_view is None
        assert not window._save_action.isEnabled()
        assert not window._rotate_cw_action.isEnabled()
    finally:
        window.close()


def test_cancel_keeps_dirty_tab(qapp, multipage_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        window.rotate_clockwise()  # dirty
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel
        )
        window._close_tab(0)
        assert window._tabs.count() == 1  # cancel kept it
    finally:
        window.close()


def test_discard_closes_dirty_tab(qapp, multipage_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        window.rotate_clockwise()
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard
        )
        window._close_tab(0)
        assert window._tabs.count() == 0
    finally:
        window.close()


def test_save_on_close_persists(qapp, multipage_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        window.go_to_page(0)
        window.delete_current_page()  # dirty; now 4 pages
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Save
        )
        window._close_tab(0)
        assert window._tabs.count() == 0
    finally:
        window.close()

    doc = pymupdf.open(str(multipage_pdf))
    try:
        assert doc.page_count == 4  # saved in place before closing
    finally:
        doc.close()
