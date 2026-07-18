"""Offscreen tests for page-edit UI wiring (rotate, delete, move, insert)."""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def test_rotate_clockwise_mutates_current_page(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(1)
        assert view.document.page_rotation(1) == 0

        window.rotate_clockwise()
        assert view.document.page_rotation(1) == 90
        assert view._canvas.has_page  # current page re-rendered after mutation

        window.rotate_counterclockwise()
        assert view.document.page_rotation(1) == 0
    finally:
        window.close()


def test_rotate_actions_require_document_and_edit_mode(qapp, multipage_pdf):
    window = MainWindow()
    try:
        assert not window._rotate_cw_action.isEnabled()
        assert not window._rotate_ccw_action.isEnabled()
        window.open_path(multipage_pdf)  # opens READ-ONLY (U0)
        assert not window._rotate_cw_action.isEnabled()
        assert not window._rotate_ccw_action.isEnabled()
        window.active_view.set_edit_mode(True)
        assert window._rotate_cw_action.isEnabled()
        assert window._rotate_ccw_action.isEnabled()
    finally:
        window.close()


def test_delete_current_page_removes_it(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        assert view.page_count == 5
        window.go_to_page(1)
        window.delete_current_page()
        assert view.page_count == 4
        assert view._thumbnails.count() == 4
        assert view._canvas.has_page
    finally:
        window.close()


def test_delete_disabled_on_single_page_document(qapp, tmp_path):
    path = tmp_path / "one.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "only page", fontsize=14)
    doc.save(str(path))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(path)
        window.active_view.set_edit_mode(True)
        assert window.active_view.page_count == 1
        assert not window._delete_action.isEnabled()  # single page, even in edit mode
    finally:
        window.close()


def test_move_page_down_swaps_and_follows(qapp, multipage_pdf, page_marker):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(0)
        window.move_page_down()
        # current follows the moved page to index 1
        assert view.current_page == 1
        # old page 0 (marker 0) now sits at index 1; old page 1 at index 0
        assert view.document._doc[0].get_text().strip() == page_marker(1)
        assert view.document._doc[1].get_text().strip() == page_marker(0)
    finally:
        window.close()


def test_move_actions_respect_bounds(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)  # at first page
        window.active_view.set_edit_mode(True)
        assert not window._move_up_action.isEnabled()
        assert window._move_down_action.isEnabled()
        window.last_page()
        assert window._move_up_action.isEnabled()
        assert not window._move_down_action.isEnabled()
    finally:
        window.close()


def test_insert_from_path_inserts_after_current(qapp, multipage_pdf, tmp_path):
    src = tmp_path / "src.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "INSERTED", fontsize=24)
    doc.save(str(src))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(1)
        view.insert_from_path(src, at=2)
        assert view.page_count == 6
        assert view.current_page == 2
        assert view.document._doc[2].get_text().strip() == "INSERTED"
        assert view._thumbnails.count() == 6
    finally:
        window.close()
