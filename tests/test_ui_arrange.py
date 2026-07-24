"""Offscreen tests for aligning + distributing the selected text BOXES
(tasks 2 & 3) — moving the boxes themselves, not their text content.

The context menu execs only when visible, so (like the other U3 tests) the
dispatch handlers are driven directly. The multi-selection is seeded straight
into ``_multi_paragraphs`` from the page geometry.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import pymupdf  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402


def _open(tmp_path, placements):
    path = tmp_path / "arrange.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, size, text in placements:
        page.insert_text((x, y), text, fontsize=size)
    doc.save(str(path))
    doc.close()
    window = MainWindow()
    window.open_path(path)
    view = window.active_view
    view.set_edit_mode(True)
    return window, view


def _select_all(view):
    view._multi_paragraphs = [(0, p) for p in view.page_geometry(0).paragraphs]
    return [p.text for _pn, p in view._multi_paragraphs]


def _bbox(view, needle):
    p = next(p for p in view.page_geometry(0).paragraphs if needle in p.text)
    return p.bbox


def test_align_left_lines_up_left_edges(qapp, tmp_path):
    window, view = _open(
        tmp_path,
        [(120, 100, 9, "alpha box"), (160, 150, 9, "beta box"), (80, 210, 9, "gamma box")],
    )
    try:
        _select_all(view)
        ys = {t: _bbox(view, t)[1] for t in ("alpha", "beta", "gamma")}
        view._align_selected_boxes("left")
        lefts = [_bbox(view, t)[0] for t in ("alpha", "beta", "gamma")]
        assert max(lefts) - min(lefts) < 0.5  # left edges equal
        assert min(lefts) == pytest.approx(80.0, abs=1.0)  # to the leftmost box
        for t in ("alpha", "beta", "gamma"):
            assert _bbox(view, t)[1] == pytest.approx(ys[t], abs=0.75)  # Y unchanged
        assert view.undo_stack.count() == 1
    finally:
        window.close()


def test_align_horizontal_centers(qapp, tmp_path):
    window, view = _open(
        tmp_path,
        [(120, 100, 9, "alpha box"), (160, 150, 9, "wider beta box"), (80, 210, 9, "gamma")],
    )
    try:
        _select_all(view)
        view._align_selected_boxes("hcenter")
        mids = [(_bbox(view, t)[0] + _bbox(view, t)[2]) / 2 for t in ("alpha", "beta", "gamma")]
        assert max(mids) - min(mids) < 0.5  # centres equal
    finally:
        window.close()


def test_align_top_lines_up_top_edges(qapp, tmp_path):
    window, view = _open(
        tmp_path,
        [(80, 100, 9, "alpha"), (200, 140, 9, "beta"), (330, 180, 9, "gamma")],
    )
    try:
        _select_all(view)
        view._align_selected_boxes("top")
        tops = [_bbox(view, t)[1] for t in ("alpha", "beta", "gamma")]
        assert max(tops) - min(tops) < 0.5  # top edges equal
        assert min(tops) == pytest.approx(_bbox(view, "alpha")[1], abs=1.0)
    finally:
        window.close()


def test_distribute_vertically_equal_gaps_with_differing_heights(qapp, tmp_path):
    window, view = _open(
        tmp_path,
        [
            (80, 100, 8, "top small"),
            (80, 150, 20, "middle tall"),
            (80, 210, 8, "third small"),
            (80, 300, 8, "bottom small"),
        ],
    )
    try:
        _select_all(view)
        first_top = _bbox(view, "top small")[1]
        last_bottom = _bbox(view, "bottom small")[3]
        view._distribute_selected_boxes("v")

        boxes = sorted(
            (_bbox(view, t) for t in ("top small", "middle tall", "third small", "bottom small")),
            key=lambda b: b[1],
        )
        # gap = next box's top minus current box's bottom.
        gaps = [boxes[i + 1][1] - boxes[i][3] for i in range(len(boxes) - 1)]
        assert max(gaps) - min(gaps) < 0.6  # equal edge-to-edge gaps
        # Extremes pinned.
        assert boxes[0][1] == pytest.approx(first_top, abs=0.75)
        assert boxes[-1][3] == pytest.approx(last_bottom, abs=0.75)
        assert view.undo_stack.count() == 1
    finally:
        window.close()


def test_distribute_horizontally_equal_gaps(qapp, tmp_path):
    window, view = _open(
        tmp_path,
        [
            (60, 120, 9, "AAAA"),
            (150, 120, 9, "BBBBBBBB"),
            (250, 120, 9, "CC"),
            (400, 120, 9, "DDDD"),
        ],
    )
    try:
        _select_all(view)
        left0 = _bbox(view, "AAAA")[0]
        right3 = _bbox(view, "DDDD")[2]
        view._distribute_selected_boxes("h")
        boxes = sorted(
            (_bbox(view, t) for t in ("AAAA", "BBBBBBBB", "CC", "DDDD")), key=lambda b: b[0]
        )
        gaps = [boxes[i + 1][0] - boxes[i][2] for i in range(len(boxes) - 1)]
        assert max(gaps) - min(gaps) < 0.6
        assert boxes[0][0] == pytest.approx(left0, abs=0.75)
        assert boxes[-1][2] == pytest.approx(right3, abs=0.75)
    finally:
        window.close()


def test_distribute_needs_three_boxes(qapp, tmp_path):
    window, view = _open(tmp_path, [(80, 100, 9, "only one"), (80, 200, 9, "and two")])
    try:
        _select_all(view)
        warnings: list[str] = []
        view.editWarning.connect(warnings.append)
        view._distribute_selected_boxes("v")
        assert view.undo_stack.count() == 0
        assert any("three or more" in m for m in warnings)
    finally:
        window.close()


def test_align_left_is_on_screen_left_on_a_rotated_page(qapp, tmp_path):
    """Review finding: align/distribute must work in ON-SCREEN axes. On a
    /Rotate 90 page, 'Align left' must share the boxes' on-screen left edge —
    which is NOT the page-space left edge."""
    from pdfapp import page_coords

    path = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.set_rotation(90)
    for x, y in ((120, 100), (160, 150), (80, 210)):
        page.insert_text((x, y), f"box {x}", fontsize=9)
    doc.save(str(path))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        view.set_edit_mode(True)
        assert view.document.page_rotation(0) == 90
        _select_all(view)
        view._align_selected_boxes("left")

        # Convert each box's new bbox to SCENE space and assert equal left edges.
        def scene_left(p):
            r = page_coords.page_rect_to_scene(
                p.bbox,
                render_zoom=view._canvas.render_zoom,
                rotation=90,
                page_size_pts=view.document.page_size(0),
            )
            return r[0]

        lefts = [scene_left(p) for p in view.page_geometry(0).paragraphs]
        assert max(lefts) - min(lefts) < 1.0  # aligned on the ON-SCREEN left
    finally:
        window.close()


def test_delete_selected_boxes_removes_them_all_in_one_step(qapp, tmp_path):
    """User request (2026-07-24): delete ALL selected text boxes at once."""
    window, view = _open(
        tmp_path,
        [(80, 100, 9, "alpha box"), (80, 150, 9, "beta box"), (80, 210, 9, "gamma box")],
    )
    try:
        _select_all(view)
        assert len(view._multi_paragraphs) == 3
        view._delete_selected_paragraphs()

        remaining = {p.text for p in view.page_geometry(0).paragraphs}
        assert "alpha box" not in remaining
        assert "beta box" not in remaining
        assert "gamma box" not in remaining
        assert view.undo_stack.count() == 1  # ONE undoable step

        view.undo_stack.undo()
        restored = {p.text for p in view.page_geometry(0).paragraphs}
        assert {"alpha box", "beta box", "gamma box"} <= restored
    finally:
        window.close()


def test_delete_key_deletes_a_multi_selection(qapp, tmp_path):
    """The Delete key removes a selected box group too, not just via the menu."""
    window, view = _open(tmp_path, [(80, 100, 9, "one box"), (80, 160, 9, "two box")])
    try:
        _select_all(view)
        view._on_delete_selection()  # what Delete/Backspace triggers
        remaining = {p.text for p in view.page_geometry(0).paragraphs}
        assert "one box" not in remaining and "two box" not in remaining
        assert view.undo_stack.count() == 1
    finally:
        window.close()


def test_align_survives_undo(qapp, tmp_path):
    window, view = _open(
        tmp_path, [(120, 100, 9, "alpha"), (160, 150, 9, "beta"), (80, 210, 9, "gamma")]
    )
    try:
        _select_all(view)
        before = {t: _bbox(view, t)[0] for t in ("alpha", "beta", "gamma")}
        view._align_selected_boxes("left")
        view.undo_stack.undo()
        after = {t: _bbox(view, t)[0] for t in ("alpha", "beta", "gamma")}
        for t in before:
            assert after[t] == pytest.approx(before[t], abs=0.75)  # restored
    finally:
        window.close()
