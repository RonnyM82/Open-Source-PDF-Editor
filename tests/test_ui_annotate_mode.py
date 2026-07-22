"""Offscreen tests for annotating in MARKUP mode (A2).

Highlight / comment / callout now work WITHOUT edit mode; content edits stay
inert until edit mode. These are the positive counterparts to the inertness
tests in test_ui_edit_mode.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402


def _markup_view(window, path):
    """Open a document and leave it in the default MARKUP mode (no edit)."""
    window.open_path(path)
    view = window.active_view
    view._canvas.resize(800, 600)
    return view


def _span(view, text):
    return next(s for s in view.document.text_spans(0) if s.text.strip() == text)


def _scene_center(view, bbox):
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return page_to_scene(
        cx,
        cy,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )


def _annot_count(view, page=0):
    return len(list(view.document._doc[page].annots()))


# --- highlight ----------------------------------------------------------------


def test_highlight_region_commits_in_markup(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _markup_view(window, quote_pdf.path)
        assert view.edit_mode is False
        span = _span(view, quote_pdf.price)
        z = view._canvas.render_zoom
        rot = view.document.page_rotation(0)
        size = view.document.page_size(0)
        x0, y0, x1, y1 = span.bbox
        s0 = page_to_scene(x0 - 1, y0 - 1, render_zoom=z, rotation=rot, page_size_pts=size)
        s1 = page_to_scene(x1 + 1, y1 + 1, render_zoom=z, rotation=rot, page_size_pts=size)

        view._on_region_selected(s0[0], s0[1], s1[0], s1[1])
        assert _annot_count(view) >= 1  # highlight landed
        assert view.undo_stack.count() == 1
        assert view.dirty

        view.undo_stack.undo()
        assert _annot_count(view) == 0  # non-destructive, fully reversed
    finally:
        window.close()


def test_highlight_span_via_dispatch_in_markup(qapp, quote_pdf):
    """The markup context menu's 'Highlight this text' dispatch works in markup."""
    window = MainWindow()
    try:
        view = _markup_view(window, quote_pdf.path)
        span = _span(view, quote_pdf.price)
        view._highlight_rect(0, span.bbox)
        assert _annot_count(view) >= 1
        assert view.undo_stack.count() == 1
    finally:
        window.close()


# --- comment lifecycle in markup ----------------------------------------------


def _place_comment(view, text, at=(200.0, 400.0)):
    z = view._canvas.render_zoom
    view.begin_insert_comment()
    view._canvas.insertPointSelected.emit(at[0] * z, at[1] * z)
    view._para_editor.setPlainText(text)
    view._para_editor._commit()
    return view.document.comments(0)[0]


def test_comment_placed_and_edited_in_markup(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _markup_view(window, quote_pdf.path)
        comment = _place_comment(view, "note one")
        assert view.edit_mode is False  # still markup
        assert len(view.document.comments(0)) == 1

        # Double-click edits it (markup floats on top).
        view._on_point_activated(*_scene_center(view, comment.rect))
        assert not view._para_editor.isHidden()
        view._para_editor.setPlainText("note two")
        view._para_editor._commit()
        assert view.document.comments(0)[0].text == "note two"
    finally:
        window.close()


def test_comment_moved_and_deleted_in_markup(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _markup_view(window, quote_pdf.path)
        comment = _place_comment(view, "drag + delete me")
        z = view._canvas.render_zoom
        cx, cy = comment.rect[0] + 5.0, comment.rect[1] + 5.0

        # A plain first press selects a comment AND accepts the move drag.
        view._on_select_drag_started(cx * z, cy * z)
        assert view._selection is not None and view._selection[0] == "comment"
        assert view._move_comment is not None
        view._on_move_drag_finished(cx * z, cy * z, cx * z, (cy + 60.0) * z)
        moved = view.document.comments(0)[0]
        assert moved.rect[1] > comment.rect[1]  # moved down

        # Delete removes it (markup).
        view._selection = ("comment", 0, moved)
        view._on_delete_selection()
        assert view.document.comments(0) == []
    finally:
        window.close()


def test_callout_two_click_in_markup(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _markup_view(window, quote_pdf.path)
        z = view._canvas.render_zoom
        view.begin_insert_callout()
        view._canvas.insertPointSelected.emit(120 * z, 300 * z)  # target
        assert view.armed_action == "callout_box"
        view._canvas.insertPointSelected.emit(260 * z, 420 * z)  # box
        view._para_editor.setPlainText("what is this line item?")
        view._para_editor._commit()
        comments = view.document.comments(0)
        assert len(comments) == 1
        assert comments[0].kind == "callout"
    finally:
        window.close()


# --- content stays inert in markup --------------------------------------------


def test_content_edits_inert_in_markup(qapp, quote_pdf, sample_png, monkeypatch):
    window = MainWindow()
    try:
        view = _markup_view(window, quote_pdf.path)
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)
        span = _span(view, quote_pdf.price)

        # Double-click on plain text selects a word (X4), never opens an editor.
        view._on_point_activated(*_scene_center(view, span.bbox))
        assert view._editor.isHidden()
        assert view._para_editor.isHidden()
        assert view._text_selection is not None  # a word got selected instead

        # Content insert/delete dispatch is inert in markup.
        view.begin_insert_text()
        assert view._click_action is None
        view._insert_text_at_point(300.0, 400.0)
        assert view._para_editor.isHidden()
        para = view.document.paragraph_at(
            0, (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
        )
        view._delete_paragraph_at(0, para)
        assert view.undo_stack.count() == 0  # nothing mutated
    finally:
        window.close()
