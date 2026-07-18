"""Offscreen tests for the U3 context-menu dispatch paths.

The menu itself execs only when the view is visible (existing pattern); the
dispatch methods it routes to are tested directly, like the E9.2 image menu.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402


def _scene_point(view, px, py):
    return page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )


def test_insert_text_at_point_opens_editor_there(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._insert_text_at_point(*_scene_point(view, 300.0, 400.0))
        assert view._para_editor.is_editing
        page_index, point = view._pending_insert
        assert page_index == 0
        assert point[0] == pytest.approx(300.0, abs=0.5)
        assert point[1] == pytest.approx(400.0, abs=0.5)
        view._para_editor.cancel()
    finally:
        window.close()


def test_insert_image_at_point_places_it(qapp, quote_pdf, sample_png, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)
        assert len(view.document.images(0)) == 1
        view._insert_image_at_point(0, 300.0, 400.0)
        images = view.document.images(0)
        assert len(images) == 2
        placed = min(images, key=lambda i: abs(i.bbox[0] - 300.0))
        assert placed.bbox[0] == pytest.approx(300.0, abs=1.0)
        view.undo_stack.undo()
        assert len(view.document.images(0)) == 1
    finally:
        window.close()


def test_highlight_rect_adds_annotation_one_undo_step(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        assert len(list(view.document._doc[0].annots())) == 0
        view._highlight_rect(0, span.bbox)
        assert len(list(view.document._doc[0].annots())) == 1
        assert view.undo_stack.count() == 1
        view.undo_stack.undo()
        assert len(list(view.document._doc[0].annots())) == 0
    finally:
        window.close()


def test_delete_text_box_from_context_menu(qapp, quote_pdf):
    """User request (2026-07-18): delete a whole text block from the context
    menu instead of opening the editor and emptying it — one undoable step."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        para = view.document.paragraph_at(0, cx, cy)
        assert para is not None

        view._delete_paragraph_at(0, para)
        assert view.document.paragraph_at(0, cx, cy) is None
        assert all(s.text.strip() != quote_pdf.price for s in view.document.text_spans(0))
        assert view.undo_stack.count() == 1

        view.undo_stack.undo()
        assert any(s.text.strip() == quote_pdf.price for s in view.document.text_spans(0))
    finally:
        window.close()


def test_delete_text_box_dissolves_registry_box(qapp, quote_pdf):
    """Deleting an INSERTED text box drops its registry record with it (the
    same one-command atomicity as an emptied editor commit)."""
    from pdfapp.page_geometry import hover_target

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._pending_insert = (0, (300.0, 500.0))
        view._on_paragraph_committed("BOXED words")
        assert len(view.document.boxes(0)) == 1

        target = hover_target(view.page_geometry(0), 305.0, 498.0)
        assert target is not None and target.kind == "text"
        view._delete_paragraph_at(0, target.payload)
        assert view.document.boxes(0) == []
        assert all("BOXED" not in s.text for s in view.document.text_spans(0))

        view.undo_stack.undo()  # the delete only — insert stays undone/redone apart
        assert len(view.document.boxes(0)) == 1
        assert any("BOXED" in s.text for s in view.document.text_spans(0))
    finally:
        window.close()


def test_context_dispatch_methods_gate_on_read_only(qapp, quote_pdf, sample_png, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)
        view._insert_text_at_point(*_scene_point(view, 300.0, 400.0))
        assert not view._para_editor.is_editing
        view._insert_image_at_point(0, 300.0, 400.0)
        assert len(view.document.images(0)) == 1  # unchanged
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        para = view.document.paragraph_at(
            0, (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
        )
        view._delete_paragraph_at(0, para)  # read-only: inert
        assert view.undo_stack.count() == 0
        assert any(s.text.strip() == quote_pdf.price for s in view.document.text_spans(0))
    finally:
        window.close()
