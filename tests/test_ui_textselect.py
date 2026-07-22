"""Offscreen tests for read-only window/marquee text selection + copy (X4).

Read-only mode only: a plain drag draws a rectangle and selects the text
inside it, a double-click selects a word, Ctrl+C / the read-only context menu
copy it, Esc clears it. All of it is INERT in edit mode (where the same
gestures drive U6 select/move and open the in-place editor) and clears on page
change / mode entry. page_coords maps the rects, so it works on rotated pages.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_rect_to_scene, page_to_scene  # noqa: E402


def _scene_point(view, px, py, page=0):
    return page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(page),
        page_size_pts=view.document.page_size(page),
    )


def _line_text(line):
    return " ".join(w.text for w in line)


def _line_index(lines, needle):
    return next(i for i, ln in enumerate(lines) if needle in _line_text(ln))


def _word_center(word):
    x0, y0, x1, y1 = word.bbox
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def _box_around(*words, pad=2.0):
    return (
        min(w.bbox[0] for w in words) - pad,
        min(w.bbox[1] for w in words) - pad,
        max(w.bbox[2] for w in words) + pad,
        max(w.bbox[3] for w in words) + pad,
    )


def _drag_box(view, x0, y0, x1, y1):
    """Simulate a read-only marquee drag from page (x0,y0) to (x1,y1)."""
    view._on_select_drag_started(*_scene_point(view, x0, y0))
    view._on_text_select_finished(*_scene_point(view, x1, y1))


def _ctrl_c(widget):
    widget.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    )


def _table_pdf(tmp_path):
    """A multi-line description cell beside a separate right column on shared
    baselines — the table hazard from the user's screenshot."""
    import pymupdf

    path = tmp_path / "table.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for i, t in enumerate(
        ["Description first line here", "Description second line more", "Third and final line"]
    ):
        page.insert_text((72, 120 + i * 8), t, fontname="helv", fontsize=7)
    page.insert_text((300, 120), "PARTNO-123", fontname="helv", fontsize=7)
    page.insert_text((430, 120), "1,185.47", fontname="helv", fontsize=7)
    doc.save(str(path))
    doc.close()
    return path


# --- hover ------------------------------------------------------------------


def test_ibeam_cursor_over_word_in_read_only(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        assert not view.edit_mode
        word = view.document.text_lines(0)[1][0]
        view._on_hover_moved(*_scene_point(view, *_word_center(word)))
        assert view._canvas._text_hover is True
        assert view._canvas.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
        view._on_hover_moved(*_scene_point(view, 5.0, 5.0))  # off any word
        assert view._canvas._text_hover is False
    finally:
        window.close()


def test_no_ibeam_in_edit_mode(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        view.set_edit_mode(True)
        word = view.document.text_lines(0)[1][0]
        view._on_hover_moved(*_scene_point(view, *_word_center(word)))
        assert view._canvas._text_hover is False
    finally:
        window.close()


def test_entering_edit_mode_resets_the_ibeam(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = view.document.text_lines(0)[1][0]
        view._on_hover_moved(*_scene_point(view, *_word_center(word)))
        assert view._canvas._text_hover is True
        view.set_edit_mode(True)
        assert view._canvas._text_hover is False
    finally:
        window.close()


# --- marquee drag -----------------------------------------------------------


def test_marquee_selects_the_lines_inside_the_box_and_copies(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        lines = view.document.text_lines(0)
        i = _line_index(lines, "line 0.")
        j = _line_index(lines, "line 1.")
        _drag_box(view, *_box_around(*lines[i], *lines[j]))
        assert view._text_selection is not None
        assert view._canvas._text_selection_rects  # chrome pushed
        view.copy_selection()
        text = qapp.clipboard().text()
        assert text == "Lorem ipsum dolor sit amet, line 0.\nLorem ipsum dolor sit amet, line 1."
    finally:
        window.close()


def test_marquee_over_one_column_excludes_the_other_columns(qapp, tmp_path):
    """The user's fix: a box over the description column copies only that
    column, never its row-mates in the part-number / price columns."""
    window = MainWindow()
    try:
        window.open_path(_table_pdf(tmp_path))
        view = window.active_view
        _drag_box(view, 60.0, 110.0, 210.0, 150.0)  # a tall, narrow box over col 1
        view.copy_selection()
        text = qapp.clipboard().text()
        assert "Description first line here" in text
        assert "PARTNO" not in text and "1,185" not in text
    finally:
        window.close()


def test_backward_box_equals_forward_box(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        lines = view.document.text_lines(0)
        i = _line_index(lines, "line 0.")
        j = _line_index(lines, "line 1.")
        x0, y0, x1, y1 = _box_around(*lines[i], *lines[j])

        _drag_box(view, x0, y0, x1, y1)
        view.copy_selection()
        forward = qapp.clipboard().text()
        _drag_box(view, x1, y1, x0, y0)  # opposite corners
        view.copy_selection()
        backward = qapp.clipboard().text()
        assert forward == backward and forward
    finally:
        window.close()


def test_live_drag_grows_the_selection(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        lines = view.document.text_lines(0)
        i = _line_index(lines, "line 0.")
        j = _line_index(lines, "line 2.")
        x0, y0, _x1, _y1 = _box_around(*lines[i])
        view._on_select_drag_started(*_scene_point(view, x0, y0))
        # small box over the first line only
        small = _box_around(*lines[i])
        view._on_text_select_moved(*_scene_point(view, small[2], small[3]))
        one = len(view._text_selection or [])
        # extend down to include three lines
        big = _box_around(*lines[i], *lines[j])
        view._on_text_select_moved(*_scene_point(view, big[2], big[3]))
        assert len(view._text_selection or []) > one
    finally:
        window.close()


def test_marquee_shows_a_rubber_band(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = view.document.text_lines(0)[1][0]
        view._on_select_drag_started(*_scene_point(view, *_word_center(word)))
        assert view._canvas._move_band is not None  # marquee band created
        assert view._canvas._text_select_press is not None  # drag accepted
    finally:
        window.close()


# --- double-click word ------------------------------------------------------


def test_double_click_selects_word(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = next(w for line in view.document.text_lines(0) for w in line if w.text == "dolor")
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        assert view._text_selection is not None
        view.copy_selection()
        assert qapp.clipboard().text() == "dolor"
    finally:
        window.close()


def test_double_click_blank_clears_selection(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = view.document.text_lines(0)[1][0]
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        assert view._text_selection is not None
        view._on_point_activated(*_scene_point(view, 5.0, 5.0), False)  # blank
        assert view._text_selection is None
    finally:
        window.close()


# --- copy paths -------------------------------------------------------------


def test_ctrl_c_on_canvas_copies_selection(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = next(w for line in view.document.text_lines(0) for w in line if w.text == "ipsum")
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        qapp.clipboard().setText("STALE")
        _ctrl_c(view._canvas)  # Ctrl+C -> copyRequested -> copy_selection
        assert qapp.clipboard().text() == "ipsum"
    finally:
        window.close()


def test_copy_is_noop_without_selection(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        qapp.clipboard().setText("UNCHANGED")
        view.copy_selection()
        assert qapp.clipboard().text() == "UNCHANGED"
        assert view.undo_stack.isClean()  # a pure read
    finally:
        window.close()


def test_comment_text_never_copied(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.document.add_comment(0, (300.0, 500.0, 460.0, 540.0), "SECRETNOTE", author="t")
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        cx, cy = (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
        view._on_point_activated(*_scene_point(view, cx, cy), False)  # select the price word
        view.copy_selection()
        text = qapp.clipboard().text()
        assert text and "SECRETNOTE" not in text
    finally:
        window.close()


# --- Esc chain + lifecycle --------------------------------------------------


def test_esc_clears_text_selection(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = view.document.text_lines(0)[1][0]
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        assert view._text_selection is not None
        view._on_escape()
        assert view._text_selection is None
        assert view._canvas._text_selection_rects == []
    finally:
        window.close()


def test_esc_clears_selection_before_closing_search(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        view.open_search()
        word = view.document.text_lines(0)[1][0]
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        assert view._text_selection is not None

        view._on_escape()  # 1) clears the selection
        assert view._text_selection is None
        assert not view._search_bar.isHidden()

        view._on_escape()  # 2) closes the search bar
        assert view._search_bar.isHidden()
    finally:
        window.close()


def test_selection_clears_on_page_change(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = view.document.text_lines(0)[1][0]
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        assert view._text_selection is not None
        view.next_page()
        assert view._text_selection is None
        assert view._canvas._text_selection_rects == []
    finally:
        window.close()


def test_selection_clears_on_entering_edit_mode(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        word = view.document.text_lines(0)[1][0]
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        assert view._text_selection is not None
        view.set_edit_mode(True)
        assert view._text_selection is None
    finally:
        window.close()


# --- inertness in edit mode -------------------------------------------------


def test_edit_mode_gestures_do_no_text_selection_and_still_drive_u6(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        cx, cy = (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
        point = _scene_point(view, cx, cy)

        view._on_select_drag_started(*point)  # a plain press in edit mode
        assert view._text_selection is None  # no text selection...
        assert view._selection is not None  # ...still drives U6 select
        assert view._canvas._text_selection_rects == []

        view._on_point_activated(*point, False)  # double-click opens the editor
        assert view._text_selection is None
        assert view._editor.is_editing or view._para_editor.is_editing
        view._editor.cancel()
        view._para_editor.cancel()
    finally:
        window.close()


# --- scanned page + rotation ------------------------------------------------


def test_scanned_page_has_nothing_to_select(qapp, ocr_pdf):
    window = MainWindow()
    try:
        window.open_path(ocr_pdf.path)
        view = window.active_view
        assert not view.edit_mode
        view._on_select_drag_started(*_scene_point(view, 100.0, 120.0))
        assert view._text_selection is None
        assert view._canvas._text_select_press is None  # drag not accepted
    finally:
        window.close()


def test_selection_rects_map_on_a_rotated_page(qapp, text_pdf):
    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        view.set_edit_mode(True)
        window.rotate_clockwise()  # /Rotate 90
        assert view.document.page_rotation(0) == 90
        view.set_edit_mode(False)  # back to read-only for selection

        word = next(w for line in view.document.text_lines(0) for w in line if w.text == "dolor")
        view._on_point_activated(*_scene_point(view, *_word_center(word)), False)
        assert len(view._canvas._text_selection_rects) == 1
        rect = view._canvas._text_selection_rects[0]
        expected = page_rect_to_scene(
            word.bbox,
            render_zoom=view._canvas.render_zoom,
            rotation=90,
            page_size_pts=view.document.page_size(0),
        )
        assert rect.left() == pytest.approx(expected[0], abs=0.01)
        assert rect.top() == pytest.approx(expected[1], abs=0.01)
        assert not view._canvas.grab().isNull()  # paints without crashing
    finally:
        window.close()
