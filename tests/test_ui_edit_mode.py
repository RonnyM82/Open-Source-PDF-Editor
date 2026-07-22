"""Offscreen tests for the Markup vs Edit mode container (U0 / A2).

Documents open in MARKUP mode: annotations (highlight / comment / callout) and
read features (select, copy, find) work, but CONTENT-edit entry points
(edit/delete text, insert text/image, image ops, page ops) are inert until the
user switches that document to Edit mode. Navigation, zoom and save never gate.
Chrome (toolbar toggle, action enablement, the status-bar mode label) follows
the ACTIVE tab's mode.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402


def _find_span(view, text):
    return next(s for s in view.document.text_spans(0) if s.text.strip() == text)


def _scene_center_of(view, bbox):
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return page_to_scene(
        cx,
        cy,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )


def test_documents_open_in_markup_mode(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        assert view.edit_mode is False
        assert window._edit_mode_action.isEnabled()
        assert not window._edit_mode_action.isChecked()
        assert window._mode_label.text() == "Markup"
        # Content-edit actions are inert until edit mode...
        for action in window._page_edit_actions:
            assert not action.isEnabled()
        # ...but annotation actions are available in Markup mode.
        for action in window._annotate_actions:
            assert action.isEnabled()
    finally:
        window.close()


def test_double_click_is_inert_until_edit_mode(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        span = _find_span(view, quote_pdf.price)
        point = _scene_center_of(view, span.bbox)
        view._on_point_activated(*point)
        assert view._editor.isHidden()  # read-only: nothing opens
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # target the single span here
        view._on_point_activated(*point)
        assert not view._editor.isHidden()
        assert view._editor.text() == span.text
    finally:
        window.close()


def test_content_armed_modes_inert_in_markup(qapp, quote_pdf, sample_png, monkeypatch):
    """CONTENT insert (text/image) stays inert in Markup mode; only edit mode
    arms it. Annotation arming is covered separately (it works in Markup)."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)

        view.begin_insert_text()
        assert view._click_action is None
        assert not view._canvas._insert_armed
        view.begin_insert_image()
        assert view._click_action is None
        assert not view._canvas._insert_armed

        view.set_edit_mode(True)
        view.begin_insert_text()
        assert view._click_action == ("text", None)
        assert view._canvas._insert_armed
    finally:
        window.close()


def test_annotations_arm_in_markup_mode(qapp, quote_pdf):
    """Highlight / comment / callout arm WITHOUT edit mode (markup, A2)."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        assert view.edit_mode is False

        view.begin_highlight()
        assert view._canvas._region_armed
        assert view.armed_action == "highlight"
        view.cancel_armed_mode()

        view.begin_insert_comment()
        assert view._canvas._insert_armed
        assert view.armed_action == "comment"
        view.cancel_armed_mode()

        view.begin_insert_callout()
        assert view.armed_action == "callout_target"
    finally:
        window.close()


def test_leaving_edit_mode_disarms_pending_modes(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.begin_insert_text()
        assert view._canvas._insert_armed
        view.set_edit_mode(False)
        assert view._click_action is None
        assert not view._canvas._insert_armed
    finally:
        window.close()


def test_ctrl_drag_move_inert_in_read_only(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        span = _find_span(view, quote_pdf.price)
        point = _scene_center_of(view, span.bbox)
        view._on_move_drag_started(*point)  # Ctrl+press over quote text
        assert view._move_paragraph is None
        assert view._canvas._move_base_rect is None
        view.set_edit_mode(True)
        view._on_move_drag_started(*point)
        assert view._move_paragraph is not None
    finally:
        window.close()


def test_context_menu_inert_in_read_only(qapp, quote_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        point = _scene_center_of(view, view.document.images(0)[0].bbox)
        calls: list = []
        monkeypatch.setattr(view._doc, "image_at", lambda *a: calls.append(a) or None)

        view._on_context_menu(*point)
        assert calls == []  # read-only returns before the hit-test
        view.set_edit_mode(True)
        view._on_context_menu(*point)  # offscreen: stub returns None, no menu execs
        assert len(calls) == 1
    finally:
        window.close()


def test_leaving_edit_mode_commits_open_editor(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # target the single span here
        span = _find_span(view, quote_pdf.price)
        view._on_point_activated(*_scene_center_of(view, span.bbox))
        assert view._editor.is_editing
        view._editor.setPlainText("456.00")
        view.set_edit_mode(False)
        assert not view._editor.is_editing  # committed, not cancelled
        assert view.undo_stack.count() == 1
        assert view.dirty
    finally:
        window.close()


def test_mode_chrome_follows_active_tab(qapp, quote_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        first = window.active_view
        window.open_path(multipage_pdf)
        second = window.active_view
        assert first is not second

        window._edit_mode_action.setChecked(True)  # toggles the ACTIVE (second) tab
        assert second.edit_mode is True
        assert first.edit_mode is False
        assert window._mode_label.text() == "Editing"
        assert window._insert_text_action.isEnabled()

        window._tabs.setCurrentWidget(first)
        assert not window._edit_mode_action.isChecked()
        assert window._mode_label.text() == "Markup"
        assert not window._insert_text_action.isEnabled()  # content: edit-mode only

        window._tabs.setCurrentWidget(second)  # per-document mode survives
        assert window._edit_mode_action.isChecked()
        assert window._mode_label.text() == "Editing"
    finally:
        window.close()


def test_undo_available_in_markup_after_annotation(qapp, quote_pdf):
    """Annotations mutate in Markup mode, so undo/redo track the stack there —
    not parked as in the old read-only model."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        assert view.edit_mode is False
        assert not window._undo_action.isEnabled()  # nothing to undo yet

        span = _find_span(view, quote_pdf.price)
        view._highlight_rect(0, span.bbox)  # a markup mutation
        window._sync_chrome()
        assert view.undo_stack.count() == 1
        assert window._undo_action.isEnabled()  # undoable in Markup mode

        view.undo_stack.undo()
        window._sync_chrome()
        assert window._redo_action.isEnabled()
    finally:
        window.close()


def test_navigation_zoom_save_never_gate(qapp, multipage_pdf, tmp_path):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        assert view.edit_mode is False
        window.next_page()
        assert view.current_page == 1
        assert window._zoom_in_action.isEnabled()
        assert window._save_as_action.isEnabled()
        assert window._print_action.isEnabled()
        out = tmp_path / "copy.pdf"
        assert view.save_as_path(out)
        assert out.exists()
    finally:
        window.close()
