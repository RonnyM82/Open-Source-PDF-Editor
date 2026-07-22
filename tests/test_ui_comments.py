"""Offscreen tests for review-comment interactions (E11.2)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_geometry import hover_target  # noqa: E402


def _view(window, path):
    window.open_path(path)
    view = window.active_view
    view.set_edit_mode(True)
    view._canvas.resize(800, 600)
    return view


def test_insert_comment_click_to_place_and_undo(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _view(window, quote_pdf.path)
        window.insert_comment()  # menu action arms click-to-place
        assert view.armed_action == "comment"
        assert window._insert_comment_action.isChecked()

        z = view._canvas.render_zoom
        view._canvas.insertPointSelected.emit(200 * z, 400 * z)
        assert not view._para_editor.isHidden()
        view._para_editor.setPlainText("check this against the PO")
        view._para_editor._commit()

        comments = view.document.comments(0)
        assert len(comments) == 1
        assert comments[0].text == "check this against the PO"
        assert comments[0].author  # stamped with the local user
        assert comments[0].kind == "note"
        assert view.dirty

        view.undo_stack.undo()
        assert view.document.comments(0) == []
        assert not view.dirty
    finally:
        window.close()


def test_insert_callout_two_click_flow(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _view(window, quote_pdf.path)
        window.insert_callout()
        assert view.armed_action == "callout_target"

        z = view._canvas.render_zoom
        view._canvas.insertPointSelected.emit(120 * z, 300 * z)  # click 1: target
        assert view.armed_action == "callout_box"
        view._canvas.insertPointSelected.emit(260 * z, 420 * z)  # click 2: the box
        view._para_editor.setPlainText("what is this line item?")
        view._para_editor._commit()

        comments = view.document.comments(0)
        assert len(comments) == 1
        assert comments[0].kind == "callout"
        assert view.undo_stack.count() == 1
    finally:
        window.close()


def test_comment_hover_edit_move_delete(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _view(window, quote_pdf.path)
        view._pending_comment = (0, (200.0, 400.0, 420.0, 464.0), None)
        view._on_paragraph_committed("v1")
        comment = view.document.comments(0)[0]

        # Hover: comments float on top of everything (the box shrinkwraps
        # its text — E11.5 — so probe just inside its actual rect).
        hx, hy = comment.rect[0] + 5.0, comment.rect[1] + 5.0
        target = hover_target(view.page_geometry(0), hx, hy)
        assert target is not None and target.kind == "comment"

        # Double-click edits (the dispatch the canvas routes to).
        view._begin_comment_edit(0, comment)
        view._para_editor.setPlainText("v2 — checked")
        view._para_editor._commit()
        edited = view.document.comments(0)[0]
        assert edited.text == "v2 — checked"

        # Ctrl+drag moves (a re-anchor; nothing redacted).
        z = view._canvas.render_zoom
        ex, ey = edited.rect[0] + 5.0, edited.rect[1] + 5.0
        view._on_move_drag_started(ex * z, ey * z)
        assert view._move_comment is not None
        view._on_move_drag_finished(ex * z, ey * z, ex * z, (ey + 100.0) * z)
        moved = view.document.comments(0)[0]
        assert moved.rect[1] == pytest.approx(500.0, abs=2.0)

        # Delete (the Delete-key/context-menu dispatch).
        view._delete_comment_at(0, moved.xref)
        assert view.document.comments(0) == []
    finally:
        window.close()


def test_comment_editor_empty_edit_deletes(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _view(window, quote_pdf.path)
        view._pending_comment = (0, (200.0, 400.0, 420.0, 464.0), None)
        view._on_paragraph_committed("temp note")
        comment = view.document.comments(0)[0]
        view._begin_comment_edit(0, comment)
        view._para_editor.setPlainText("")
        view._para_editor._commit()
        assert view.document.comments(0) == []  # emptied comment removed
    finally:
        window.close()


def test_search_bar_comment_toggle(qapp, quote_pdf):
    window = MainWindow()
    try:
        view = _view(window, quote_pdf.path)
        view._pending_comment = (0, (200.0, 500.0, 420.0, 564.0), None)
        view._on_paragraph_committed("FINDME_IN_COMMENT")

        view.open_search()
        view._search_bar._query.setText("FINDME_IN_COMMENT")
        view.run_search()
        assert view._search_hits == []  # excluded by default

        view._search_bar._comments_check.setChecked(True)
        view.run_search()
        assert len(view._search_hits) == 1  # opt-in finds it
    finally:
        window.close()


def test_comments_arm_in_markup_mode(qapp, quote_pdf):
    """A2: comment/callout insertion is available in Markup mode (annotations
    are decoupled from edit mode)."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view  # Markup mode by default (U0)
        assert view.edit_mode is False
        view.begin_insert_comment()
        assert view.armed_action == "comment"
        view.cancel_armed_mode()
        view.begin_insert_callout()
        assert view.armed_action == "callout_target"
    finally:
        window.close()


def test_comment_drags_on_first_press_text_stays_click_first(qapp, quote_pdf):
    """User request (2026-07-18): no select-then-drag dance for markup — a
    plain press on an UNSELECTED comment accepts the move drag immediately.
    Text keeps the click-first rule (stray drags must never move content)."""
    window = MainWindow()
    try:
        view = _view(window, quote_pdf.path)
        view._pending_comment = (0, (200.0, 400.0, 0.0, 0.0), None)
        view._on_paragraph_committed("drag me")
        comment = view.document.comments(0)[0]
        z = view._canvas.render_zoom
        cx, cy = comment.rect[0] + 5.0, comment.rect[1] + 5.0

        assert view._selection is None
        view._on_select_drag_started(cx * z, cy * z)  # FIRST press
        assert view._selection is not None and view._selection[0] == "comment"
        assert view._move_comment is not None  # drag accepted immediately
        view._on_move_drag_finished(cx * z, cy * z, cx * z, (cy + 80.0) * z)
        moved = view.document.comments(0)[0]
        assert moved.rect[1] == pytest.approx(comment.rect[1] + 80.0, abs=2.0)

        # Text: the first press only SELECTS — no move accepted.
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        tx = (span.bbox[0] + span.bbox[2]) / 2
        ty = (span.bbox[1] + span.bbox[3]) / 2
        view._on_select_drag_started(tx * z, ty * z)
        assert view._selection is not None and view._selection[0] == "text"
        assert view._move_paragraph is None
    finally:
        window.close()


def test_retarget_callout_one_click(qapp, quote_pdf):
    """User request (2026-07-18): re-point a callout's arrowhead — context
    menu arms ONE click for the new target (armed chip; Esc cancels)."""
    window = MainWindow()
    try:
        view = _view(window, quote_pdf.path)
        view._pending_comment = (0, (300.0, 150.0, 0.0, 0.0), (100.0, 300.0))
        view._on_paragraph_committed("point over there")
        comment = view.document.comments(0)[0]
        assert comment.kind == "callout"

        view.begin_retarget_callout(0, comment)
        assert view.armed_action == "retarget"
        assert not view._canvas._armed_chip.isHidden()
        z = view._canvas.render_zoom
        view._canvas.insertPointSelected.emit(220.0 * z, 500.0 * z)
        view._canvas.disarm_insert_point()  # the real click path disarms first
        moved = view.document.comments(0)[0]
        assert moved.target == pytest.approx((220.0, 500.0), abs=1.0)
        assert moved.rect[0] == pytest.approx(comment.rect[0], abs=1.0)  # box stays
        assert view.undo_stack.count() == 2  # add + retarget

        view.undo_stack.undo()
        assert view.document.comments(0)[0].target == pytest.approx((100.0, 300.0), abs=1.0)

        view.set_edit_mode(False)  # Markup mode: retarget stays available (A2)
        view.begin_retarget_callout(0, view.document.comments(0)[0])
        assert view.armed_action == "retarget"
    finally:
        window.close()
