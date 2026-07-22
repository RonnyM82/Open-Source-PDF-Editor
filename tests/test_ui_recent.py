"""Offscreen tests for the File → Open Recent fly-out."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def _recent_texts(window):
    """The file entries in the Open Recent menu (accelerators/placeholder aside)."""
    window._rebuild_recent_menu()
    texts = []
    for action in window._recent_menu.actions():
        if action.isSeparator() or not action.isEnabled():
            continue
        if action.text() == "Clear Recent Files":
            continue
        texts.append(action.text())
    return texts


def test_recent_menu_empty_placeholder(qapp):
    window = MainWindow()
    try:
        window._rebuild_recent_menu()
        actions = window._recent_menu.actions()
        assert len(actions) == 1
        assert not actions[0].isEnabled()  # "No recent files" placeholder
    finally:
        window.close()


def test_open_records_recent_most_recent_first(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        window.open_path(multipage_pdf)
        texts = _recent_texts(window)
        # Most-recent first; each row ends with the file name.
        assert texts[0].endswith(multipage_pdf.name)
        assert texts[1].endswith(text_pdf.name)
    finally:
        window.close()


def test_recent_entry_reopens_file(qapp, text_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)  # 3 pages
        window.open_path(multipage_pdf)  # 5 pages, active
        # Close both tabs so the recent entry actually re-opens.
        while window._tabs.count():
            window._tabs.removeTab(0)
        window._rebuild_recent_menu()
        for action in window._recent_menu.actions():
            if action.text().endswith(text_pdf.name):
                action.trigger()
                break
        assert window.active_view is not None
        assert window.active_view.page_count == 3
    finally:
        window.close()


def test_missing_recent_file_is_pruned(qapp, tmp_path, monkeypatch):
    import pdfapp.main_window as mw

    window = MainWindow()
    try:
        gone = tmp_path / "gone.pdf"
        window._recent_files.add(gone)
        # Silence the warning dialog.
        monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
        window._open_recent(gone)
        assert gone not in window._recent_files.entries()
    finally:
        window.close()


def test_clear_recent_empties_menu(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        assert _recent_texts(window)  # non-empty
        window._clear_recent()
        assert _recent_texts(window) == []
    finally:
        window.close()


def test_recent_menu_lives_in_file_menu(qapp):
    window = MainWindow()
    try:
        file_menu = next(
            m
            for m in window.menuBar().findChildren(type(window._window_menu))
            if m.title() == "&File"
        )
        assert window._recent_menu in [a.menu() for a in file_menu.actions() if a.menu()]
    finally:
        window.close()
