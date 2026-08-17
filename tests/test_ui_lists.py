"""List v2 UI tests (LR3): live QTextList editing in the paragraph/insert
editors — toggles, Enter continuation, Tab/Shift+Tab nesting, empty-item
end, seeding from a committed box, and the commit conversion to engine
list blocks. Offscreen; editor methods and key events driven directly (the
established pattern — dialogs/menus gate on isVisible)."""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QTextCursor  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfcore.lists import marker_fontfile  # noqa: E402
from pdfcore.textedit import paragraph_blocks  # noqa: E402

needs_marker_font = pytest.mark.skipif(
    marker_fontfile() is None, reason="no marker font on this machine"
)


def _pdf(tmp_path, lines=(), name="t.pdf"):
    path = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((100, 100 + i * 13.2), line, fontname="helv", fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _view(window, tmp_path, lines=()):
    window.open_path(_pdf(tmp_path, lines))
    view = window.active_view
    view.set_edit_mode(True)
    view._canvas.resize(800, 600)
    return view


def _geom_para(view, needle):
    return next(p for p in view.page_geometry(0).paragraphs if needle in p.text)


def _key(editor, key, mods=Qt.KeyboardModifier.NoModifier):
    editor.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


def _press_enter(editor):
    _key(editor, Qt.Key.Key_Return)


def test_toggle_list_in_editor_commits_a_real_bullet(qapp, tmp_path):
    """Open a plain paragraph, press the bullet toggle, commit: the page
    carries a marker and the box reads back as ONE bullet item."""
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["Plain text that becomes an item"])
        para = _geom_para(view, "becomes an item")
        view._begin_paragraph_edit(0, para)
        assert view.toggle_editor_list("bullet")
        assert view._para_editor.caret_list_state() == ("bullet", 0)
        view._para_editor.commit()
        assert view.undo_stack.count() == 1
        after = _geom_para(view, "becomes an item")
        specs = paragraph_blocks(after)
        assert [s.kind for s in specs] == ["bullet"]
    finally:
        window.close()


def test_toggle_only_change_is_a_real_edit_not_a_noop(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["Unchanged text, new format"])
        view._begin_paragraph_edit(0, _geom_para(view, "Unchanged"))
        view.toggle_editor_list("number")
        view._para_editor.commit()  # text unchanged, structure changed
        assert view.undo_stack.count() == 1
    finally:
        window.close()


def test_toggle_active_kind_unlists(qapp, tmp_path):
    """Acrobat: clicking the highlighted list type removes the formatting."""
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["Bullet me and back"])
        view._begin_paragraph_edit(0, _geom_para(view, "Bullet me"))
        view.toggle_editor_list("bullet")
        view.toggle_editor_list("bullet")  # active kind again -> remove
        assert view._para_editor.caret_list_state() == (None, 0)
        view._para_editor.commit()
        assert view.undo_stack.count() == 0  # back to as-opened: a no-op
    finally:
        window.close()


def test_enter_continues_the_list_one_box_one_undo(qapp, tmp_path):
    """Enter inside a list item starts the next item (live numbering); the
    whole list commits as ONE box in ONE undo step."""
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["First item with enough width to hold its siblings"])
        view._begin_paragraph_edit(0, _geom_para(view, "First item"))
        editor = view._para_editor
        view.toggle_editor_list("number")
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
        _press_enter(editor)  # Qt continues the QTextList
        editor.insertPlainText("Second item")
        states = editor.blocks_state()
        assert [s[0] for s in states] == ["number", "number"]
        assert [s[2] for s in states] == [1, 2]  # live renumbering
        editor.commit()
        assert view.undo_stack.count() == 1
        after = _geom_para(view, "First item")
        specs = paragraph_blocks(after)
        assert [(s.kind, s.ordinal) for s in specs] == [("number", 1), ("number", 2)]
        assert "Second item" in after.text  # ONE box holds both items
    finally:
        window.close()


@needs_marker_font
def test_tab_indents_and_backspace_outdents(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["Item to nest"])
        view._begin_paragraph_edit(0, _geom_para(view, "Item to nest"))
        editor = view._para_editor
        view.toggle_editor_list("bullet")
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        editor.setTextCursor(cur)
        _key(editor, Qt.Key.Key_Tab)
        assert editor.caret_list_state() == ("bullet", 1)
        _key(editor, Qt.Key.Key_Backspace)  # at block start: outdent
        assert editor.caret_list_state() == ("bullet", 0)
        _key(editor, Qt.Key.Key_Backspace)  # at level 0: unlist
        assert editor.caret_list_state() == (None, 0)
    finally:
        window.close()


def test_enter_on_empty_item_ends_the_list(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["Only item"])
        view._begin_paragraph_edit(0, _geom_para(view, "Only item"))
        editor = view._para_editor
        view.toggle_editor_list("bullet")
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
        _press_enter(editor)  # a fresh empty item
        assert editor.caret_list_state()[0] == "bullet"
        _press_enter(editor)  # Enter on the EMPTY item ends the list
        assert editor.caret_list_state() == (None, 0)
    finally:
        window.close()


def test_committed_list_box_reopens_with_live_structure(qapp, tmp_path):
    """The round trip: commit a two-item list, reopen the box — markers come
    back as QTextLists (structure), not literal text, and an unchanged
    commit is a no-op."""
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["Alpha entry"])
        view._begin_paragraph_edit(0, _geom_para(view, "Alpha entry"))
        editor = view._para_editor
        view.toggle_editor_list("number")
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
        _press_enter(editor)
        editor.insertPlainText("Beta entry")
        editor.commit()
        assert view.undo_stack.count() == 1

        again = _geom_para(view, "Alpha entry")
        view._begin_paragraph_edit(0, again)
        states = editor.blocks_state()
        assert [s[0] for s in states] == ["number", "number"]
        text = editor.toPlainText()
        assert "1." not in text and "2." not in text  # markers are structure
        editor.commit()  # unchanged
        assert view.undo_stack.count() == 1  # still just the one command
    finally:
        window.close()


@needs_marker_font
def test_insert_editor_creates_a_list_from_scratch(qapp, tmp_path):
    """The Acrobat flow replacing v1's Insert-list command: Insert text,
    toggle bullets, type items, commit — one registered box, one undo."""
    window = MainWindow()
    try:
        view = _view(window, tmp_path)
        view._pending_insert = (0, (100.0, 200.0))
        view._open_insert_editor(0, 100.0, 200.0, *view._page_point_to_scene(100.0, 200.0, 0))
        editor = view._para_editor
        editor.insertPlainText("First typed item")
        view.toggle_editor_list("bullet")
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
        _press_enter(editor)
        editor.insertPlainText("Second typed item")
        editor.commit()
        assert view.undo_stack.count() == 1
        assert len(view.document.boxes(0)) == 1  # ONE box for the whole list
        para = _geom_para(view, "First typed item")
        specs = paragraph_blocks(para)
        assert [s.kind for s in specs] == ["bullet", "bullet"]
        assert "Second typed item" in para.text
    finally:
        window.close()


# --- LR4b: moves / duplicates / merges keep the list --------------------------


def _bullet_box(view, needle):
    """Format the paragraph containing ``needle`` as a bullet item via the
    editor (the real flow), returning the re-read paragraph."""
    view._begin_paragraph_edit(0, _geom_para(view, needle))
    view.toggle_editor_list("bullet")
    view._para_editor.commit()
    return _geom_para(view, needle)


def test_moving_a_list_box_keeps_the_list(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["An item that will be moved"])
        item = _bullet_box(view, "will be moved")
        z = view._canvas.render_zoom
        cx = (item.bbox[0] + item.bbox[2]) / 2
        cy = (item.bbox[1] + item.bbox[3]) / 2
        view._on_move_drag_started(cx * z, cy * z)
        view._on_move_drag_finished(cx * z, cy * z, (cx + 60.0) * z, (cy + 40.0) * z)
        moved = _geom_para(view, "will be moved")
        assert moved.bbox[1] > item.bbox[1] + 30
        specs = paragraph_blocks(moved)
        assert [s.kind for s in specs] == ["bullet"]  # still a list at the new spot
        assert moved.hang_indent > 0
    finally:
        window.close()


def test_duplicating_a_list_box_copies_the_list(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["An item to duplicate"])
        item = _bullet_box(view, "to duplicate")
        view._duplicate_paragraph_at(0, item)
        copies = [p for p in view.page_geometry(0).paragraphs if "to duplicate" in p.text]
        assert len(copies) == 2
        for para in copies:
            assert [s.kind for s in paragraph_blocks(para)] == ["bullet"]
    finally:
        window.close()


def test_merging_two_list_boxes_stays_a_list(qapp, tmp_path):
    """Two ADJACENT inserted list boxes merge into one box that still reads
    as a two-item list (the union's blocks re-derive from the markers)."""
    window = MainWindow()
    try:
        view = _view(window, tmp_path)
        for y, text in ((200.0, "Alpha merge item"), (230.0, "Beta merge item")):
            view._pending_insert = (0, (100.0, y))
            view._on_paragraph_committed(text)
        for needle in ("Alpha merge", "Beta merge"):
            view._begin_paragraph_edit(0, _geom_para(view, needle))
            view.toggle_editor_list("number")
            view._para_editor.commit()
        a = _geom_para(view, "Alpha merge")
        b = _geom_para(view, "Beta merge")
        view._multi_paragraphs = [(0, a), (0, b)]
        view._merge_selected_paragraphs()
        merged = _geom_para(view, "Alpha merge")
        assert "Beta merge" in merged.text  # one box now
        specs = paragraph_blocks(merged)
        assert [s.kind for s in specs] == ["number", "number"]
    finally:
        window.close()


def test_shift_enter_breaks_the_line_within_an_item(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _view(window, tmp_path, ["Item with a break"])
        view._begin_paragraph_edit(0, _geom_para(view, "Item with a break"))
        editor = view._para_editor
        view.toggle_editor_list("bullet")
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
        _key(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        editor.insertPlainText("continuation line")
        # still ONE block (the soft break stays inside the item)
        assert len(editor.blocks_state()) == 1
        editor.commit()
        after = _geom_para(view, "Item with a break")
        specs = paragraph_blocks(after)
        assert [s.kind for s in specs] == ["bullet"]
        assert len(specs[0].lines) == 2  # two visual lines, one item
    finally:
        window.close()
