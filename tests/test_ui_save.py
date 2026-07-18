"""Offscreen tests for Save / Save As + dirty-state tracking (now per-view)."""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def test_mutation_sets_dirty_and_title_marker(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        assert view.dirty is False
        assert not window.windowTitle().endswith("*")

        window.rotate_clockwise()
        assert view.dirty is True
        assert window.windowTitle().endswith("*")
    finally:
        window.close()


def test_save_in_place_persists_and_clears_dirty(qapp, multipage_pdf, page_marker):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(0)
        window.delete_current_page()
        assert view.dirty is True

        window.save()  # atomic in-place
        assert view.dirty is False
    finally:
        window.close()

    # The change is on disk in the original file.
    doc = pymupdf.open(str(multipage_pdf))
    try:
        assert doc.page_count == 4
        assert doc[0].get_text().strip() == page_marker(1)
    finally:
        doc.close()


def test_save_as_writes_new_file_and_switches(qapp, multipage_pdf, tmp_path):
    out = tmp_path / "copy.pdf"
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.rotate_clockwise()
        assert view.dirty is True

        assert view.save_as_path(out) is True
        assert out.exists()
        assert view.dirty is False
        assert view.document.source == out  # now editing the saved copy
    finally:
        window.close()


def test_save_as_same_path_routes_to_in_place(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(0)
        window.delete_current_page()
        # Save As targeting the currently-open path must route to atomic in-place
        # rather than trying to overwrite the open file.
        assert view.save_as_path(multipage_pdf) is True
        assert view.dirty is False
        assert view.page_count == 4
    finally:
        window.close()


def test_locked_file_save_shows_in_use_message_and_stays_dirty(qapp, multipage_pdf, monkeypatch):
    """User report (2026-07-18): a save with the PDF open in Acrobat showed
    raw '[WinError 5] Access is denied' — the dialog must say another
    application has the file, and the document must stay dirty/usable."""
    from PySide6.QtWidgets import QMessageBox

    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.rotate_clockwise()
        assert view.dirty

        captured: list[str] = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *args: captured.append(args[2]) or None)
        monkeypatch.setattr(
            view._doc,
            "save_in_place",
            lambda: (_ for _ in ()).throw(PermissionError(13, "Access is denied")),
        )
        assert view.save() is False
        assert captured and "another application" in captured[0]
        assert multipage_pdf.name in captured[0]
        assert view.dirty  # the failed save must not clear the dirty state

        # A non-permission failure keeps the honest raw error text.
        captured.clear()
        monkeypatch.setattr(
            view._doc,
            "save_in_place",
            lambda: (_ for _ in ()).throw(RuntimeError("disk exploded")),
        )
        assert view.save() is False
        assert captured and "disk exploded" in captured[0]
    finally:
        window.close()
