"""Offscreen tests for snapshot undo: QUndoStack wiring for all mutations.

Undo restores document STATE (byte snapshots); it never reverses individual
PDF ops. All six mutation paths flow through the stack — an out-of-band
mutation would be silently resurrected by a later undo.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.undo import SnapshotCommand, undo_limit_for  # noqa: E402

# --- pure: undo depth budget (no Qt needed) --------------------------------


@pytest.mark.parametrize(
    ("file_size", "expected"),
    [
        (0, 64),  # degenerate size -> capped depth
        (300 * 1024, 64),  # small quote -> full depth
        (16 * 1024 * 1024, 8),  # 256MB // (2 * 16MB)
        (80 * 1024 * 1024, 1),  # large scan -> minimum depth
        (1024 * 1024 * 1024, 1),  # bigger than the whole budget -> still 1
    ],
)
def test_undo_limit_for(file_size, expected):
    assert undo_limit_for(file_size) == expected


def test_undo_limit_for_custom_budget():
    assert undo_limit_for(10, budget=100) == 5


# --- stack wiring -----------------------------------------------------------


def test_rotate_undo_redo(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        assert not view.dirty

        window.rotate_clockwise()
        assert view.document.page_rotation(0) == 90
        assert view.dirty

        view.undo_stack.undo()
        assert view.document.page_rotation(0) == 0
        assert not view.dirty  # back at the clean state

        view.undo_stack.redo()
        assert view.document.page_rotation(0) == 90
        assert view.dirty
        assert view._canvas.has_page  # view refreshed after each restore
    finally:
        window.close()


def test_delete_undo_restores_page(qapp, multipage_pdf, page_marker):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(1)
        window.delete_current_page()
        assert view.page_count == 4

        view.undo_stack.undo()
        assert view.page_count == 5
        assert view.document._doc[1].get_text().strip() == page_marker(1)
        assert view._thumbnails.count() == 5  # thumbnails rebuilt after restore
        assert not view.dirty
    finally:
        window.close()


def test_move_undo_restores_order(qapp, multipage_pdf, page_marker):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.go_to_page(0)
        window.move_page_down()
        assert view.document._doc[0].get_text().strip() == page_marker(1)

        view.undo_stack.undo()
        assert view.document._doc[0].get_text().strip() == page_marker(0)
    finally:
        window.close()


def test_undo_past_save_marks_dirty(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        window.rotate_clockwise()
        assert view.save()
        assert not view.dirty

        view.undo_stack.undo()  # undo past the save point
        assert view.dirty  # document differs from disk again
        assert view.document.page_rotation(0) == 0

        view.undo_stack.redo()
        assert not view.dirty  # back at the saved state
    finally:
        window.close()


def test_depth_cap_drops_oldest(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view
        view.undo_stack.setUndoLimit(2)  # legal: the stack is still empty here

        for _ in range(3):
            window.rotate_clockwise()
        assert view.document.page_rotation(0) == 270
        assert view.undo_stack.count() == 2  # oldest command dropped

        view.undo_stack.undo()
        view.undo_stack.undo()
        assert view.document.page_rotation(0) == 90  # first rotate out of reach
        assert not view.undo_stack.canUndo()
    finally:
        window.close()


def test_per_tab_stacks_isolated(qapp, multipage_pdf, text_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        first = window.active_view
        window.open_path(text_pdf)
        second = window.active_view
        assert first is not second
        assert window._undo_group.activeStack() is second.undo_stack

        window.rotate_clockwise()  # acts on the active (second) view
        assert second.dirty and not first.dirty

        window._tabs.setCurrentWidget(first)
        assert window._undo_group.activeStack() is first.undo_stack
        assert not window._undo_group.canUndo()  # first tab has no edits
    finally:
        window.close()


def test_failed_op_dropped_and_state_restored(qapp, multipage_pdf):
    window = MainWindow()
    try:
        window.open_path(multipage_pdf)
        view = window.active_view

        def boom(_doc):
            raise RuntimeError("op exploded")

        command = SnapshotCommand("Boom", view, boom)
        view.undo_stack.push(command)

        assert command.error is not None
        assert view.undo_stack.count() == 0  # obsolete command dropped by Qt
        assert not view.dirty
        assert view.page_count == 5  # document unharmed (before-bytes restored)
    finally:
        window.close()


def test_undo_actions_in_edit_menu(qapp, multipage_pdf):
    window = MainWindow()
    try:
        assert not window._undo_action.isEnabled()  # no document, no undo
        window.open_path(multipage_pdf)
        window.active_view.set_edit_mode(True)  # undo/redo park while read-only
        assert not window._undo_action.isEnabled()  # clean stack
        window.rotate_clockwise()
        assert window._undo_action.isEnabled()
        assert not window._redo_action.isEnabled()
        window._undo_action.trigger()
        assert window.active_view.document.page_rotation(0) == 0
        assert window._redo_action.isEnabled()
    finally:
        window.close()
