"""Offscreen tests for click-to-select with handles (U6).

Plain click selects the paragraph/image under it (selection chrome drawn by
drawForeground); a press on the ALREADY-selected element accepts a
move/resize drag through the same rubber-band protocol as Ctrl+drag; Delete
removes a selected image; Esc/click-away deselects. All of it is inert in
read-only (U0 rule).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402


def _scene_point(view, px, py, page=0):
    return page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(page),
        page_size_pts=view.document.page_size(page),
    )


def _span_center(view, text):
    span = next(s for s in view.document.text_spans(0) if s.text.strip() == text)
    return (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2


def _logo_center(view):
    logo = view.document.images(0)[0]
    return (logo.bbox[0] + logo.bbox[2]) / 2, (logo.bbox[1] + logo.bbox[3]) / 2


def test_selection_inert_in_read_only(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view._on_select_drag_started(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._selection is None
        assert view._canvas._selection_rect is None
    finally:
        window.close()


def test_click_selects_text_then_empty_click_deselects(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_select_drag_started(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._selection is not None
        assert view._selection[0] == "text"
        assert view._canvas._selection_rect is not None

        # A click on empty page now starts a box marquee (task 1); the clear
        # lands on release when the "drag" turns out to be a click (< 2 pt).
        empty = _scene_point(view, 10.0, 10.0)
        view._on_select_drag_started(*empty)  # press: starts the marquee
        view._on_box_marquee_finished(*empty, *empty)  # release at same point = click
        assert view._selection is None
        assert view._canvas._selection_rect is None
    finally:
        window.close()


def test_click_selects_image_and_shows_handles_chrome(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_select_drag_started(*_scene_point(view, *_logo_center(view)))
        assert view._selection[0] == "image"
        assert view._canvas._selection_kind == "image"
        assert not view._canvas.grab().isNull()  # paints border + handles
    finally:
        window.close()


def test_first_press_selects_without_accepting_a_drag(qapp, quote_pdf):
    """Click-then-drag is deliberate: a press on an UNSELECTED element only
    selects — a stray drag must never move text."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        point = _scene_point(view, *_span_center(view, quote_pdf.price))
        view._on_select_drag_started(*point)
        assert view._selection is not None
        assert view._canvas._move_base_rect is None  # no drag accepted
        assert view._move_paragraph is None

        view._on_select_drag_started(*point)  # second press: now it drags
        assert view._move_paragraph is not None
        assert view._canvas._move_base_rect is not None
    finally:
        window.close()


def _paragraph_pdf(tmp_path):
    """A clean multi-line paragraph (mirrors test_ui_text_edit's fixture —
    the generated quote's table row extracts as ONE wide line, so it can't
    isolate a single-cell move)."""
    import pymupdf

    path = tmp_path / "para.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 111), "body first line of the paragraph", fontsize=8)
    page.insert_text((72, 119), "body second line with more words", fontsize=8)
    page.insert_text((72, 127), "body third and final line", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def test_selected_paragraph_drag_moves_it(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        z = view._canvas.render_zoom
        sx, sy = 100 * z, 118 * z
        view._on_select_drag_started(sx, sy)  # select
        assert view._selection is not None and view._selection[0] == "text"
        view._on_select_drag_started(sx, sy)  # accept the drag
        assert view._move_paragraph is not None
        before = next(s for s in view.document.text_spans(0) if "body first" in s.text)

        view._on_move_drag_finished(sx, sy, sx + 50 * z, sy + 30 * z)
        after = next(s for s in view.document.text_spans(0) if "body first" in s.text)
        assert after.origin[0] == pytest.approx(before.origin[0] + 50.0, abs=0.75)
        assert after.origin[1] == pytest.approx(before.origin[1] + 30.0, abs=0.75)
        assert view._selection is None  # a mutation clears the selection
        view.undo_stack.undo()
    finally:
        window.close()


def test_selected_image_corner_drag_resizes(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        logo = view.document.images(0)[0]
        x0, y0, x1, y1 = logo.bbox
        view._on_select_drag_started(*_scene_point(view, *_logo_center(view)))  # select
        corner = _scene_point(view, x1 - 1, y1 - 1)
        view._on_select_drag_started(*corner)  # press the bottom-right corner
        assert view._resize_image is not None
        assert view._resize_image[2] == (x0, y0)  # anchored opposite

        # Drag the corner inward to shrink the image.
        target = _scene_point(view, x0 + (x1 - x0) / 2, y0 + (y1 - y0) / 2)
        view._on_move_drag_finished(*corner, *target)
        resized = view.document.images(0)[0]
        assert (resized.bbox[2] - resized.bbox[0]) < (x1 - x0) - 1
        view.undo_stack.undo()
    finally:
        window.close()


def test_delete_key_removes_selected_image_and_undo_restores(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        assert len(view.document.images(0)) == 1
        view._on_select_drag_started(*_scene_point(view, *_logo_center(view)))
        view._on_delete_selection()
        assert len(view.document.images(0)) == 0
        assert view._selection is None
        view.undo_stack.undo()
        assert len(view.document.images(0)) == 1
    finally:
        window.close()


def test_delete_key_ignores_text_selection(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_select_drag_started(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        view._on_delete_selection()
        assert view.undo_stack.count() == 0  # nothing happened
        assert view._selection is not None
    finally:
        window.close()


def test_escape_deselects(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_select_drag_started(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._selection is not None
        view._on_escape()
        assert view._selection is None
        assert view._canvas._selection_rect is None
    finally:
        window.close()


def test_escape_clears_a_pure_multi_selection(qapp, quote_pdf):
    """Esc must clear a MULTI-selection too (Ctrl/Shift+click builds one with
    _selection is None) — not only a single selection. The multi state was
    unreachable by the Esc chain before its guard was broadened."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        paras = view.document.paragraphs(0)
        view.toggle_multi_select(0, paras[0])
        view.toggle_multi_select(0, paras[1])
        assert len(view._multi_paragraphs) == 2 and view._selection is None
        assert view._canvas._multi_selection_rects  # chrome up

        view._on_escape()
        assert view._multi_paragraphs == []
        assert view._canvas._multi_selection_rects == []
    finally:
        window.close()


def test_opening_an_editor_clears_selection(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # open the span editor specifically
        point = _scene_point(view, *_span_center(view, quote_pdf.price))
        view._on_select_drag_started(*point)
        assert view._selection is not None
        view._on_point_activated(*point)  # double-click opens the span editor
        assert view._selection is None
        assert view._editor.is_editing
        view._editor.cancel()
    finally:
        window.close()


def test_leaving_edit_mode_and_page_change_deselect(qapp, quote_pdf, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        point = _scene_point(view, *_span_center(view, quote_pdf.price))
        view._on_select_drag_started(*point)
        assert view._selection is not None
        view.set_edit_mode(False)
        assert view._selection is None

        window.open_path(multipage_pdf)
        second = window.active_view
        second.set_edit_mode(True)
        # Select the page-0 text, then navigate away: selection dies with it.
        span = second.document.text_spans(0)[0]
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        second._on_select_drag_started(*_scene_point(second, cx, cy))
        assert second._selection is not None
        second.next_page()
        assert second._selection is None
    finally:
        window.close()


def test_ctrl_drag_fast_path_still_works_without_selection(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_move_drag_started(*_scene_point(view, *_logo_center(view)))
        assert view._move_image_target is not None  # accepted with no selection
        assert view._selection is None
    finally:
        window.close()
