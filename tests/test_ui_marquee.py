"""Offscreen tests for edit-mode box-marquee selection (task 1).

A drag on empty page area rubber-bands text boxes into the multi-selection.
Direction picks the mode (AutoCAD/Fusion/Inventor convention): LEFT->RIGHT =
fully enclosed (window); RIGHT->LEFT = any box touched (crossing). The live
modifier folds the marquee set in: plain replaces, Shift adds, Ctrl removes.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import pymupdf  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402


def _boxes_pdf(tmp_path):
    """Three stacked single-line boxes (a table column) + one far away."""
    path = tmp_path / "boxes.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "row one alpha", fontsize=9)
    page.insert_text((72, 140), "row two beta", fontsize=9)
    page.insert_text((72, 180), "row three gamma", fontsize=9)
    page.insert_text((360, 400), "far away box", fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _view(tmp_path):
    window = MainWindow()
    window.open_path(_boxes_pdf(tmp_path))
    view = window.active_view
    view.set_edit_mode(True)
    return window, view


def _paras_by_y(view):
    return sorted(view.page_geometry(0).paragraphs, key=lambda p: p.bbox[1])


def _scene(view, px, py):
    return page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )


def _marquee(view, page_rect, *, direction):
    """Drive the marquee finish with a page-space rect. ``direction`` picks the
    drag order: 'lr' (window) or 'rl' (crossing)."""
    x0, y0, x1, y1 = page_rect
    tl = _scene(view, x0, y0)
    br = _scene(view, x1, y1)
    if direction == "lr":
        view._on_box_marquee_finished(*tl, *br)  # press left, release right
    else:
        view._on_box_marquee_finished(*br, *tl)  # press right, release left


def _set_mods(monkeypatch, *, shift=False, ctrl=False):
    mod = Qt.KeyboardModifier.NoModifier
    if shift:
        mod |= Qt.KeyboardModifier.ShiftModifier
    if ctrl:
        mod |= Qt.KeyboardModifier.ControlModifier
    monkeypatch.setattr(
        "pdfapp.document_view.QApplication.keyboardModifiers", staticmethod(lambda: mod)
    )


def _selected_texts(view):
    return sorted(pp.text for _pn, pp in view._multi_paragraphs)


def test_window_selects_only_fully_enclosed(qapp, tmp_path, monkeypatch):
    window, view = _view(tmp_path)
    try:
        _set_mods(monkeypatch)
        paras = _paras_by_y(view)
        top, mid = paras[0], paras[1]
        # A rect that fully encloses the top two column boxes, not the third.
        rect = (60, 90, max(top.bbox[2], mid.bbox[2]) + 10, mid.bbox[3] + 5)
        _marquee(view, rect, direction="lr")
        assert _selected_texts(view) == sorted([top.text, mid.text])
    finally:
        window.close()


def test_window_excludes_a_straddled_box_that_crossing_includes(qapp, tmp_path, monkeypatch):
    window, view = _view(tmp_path)
    try:
        _set_mods(monkeypatch)
        paras = _paras_by_y(view)
        top, mid = paras[0], paras[1]
        # Cut through the MIDDLE box vertically (its bottom is outside the rect).
        rect = (60, 90, top.bbox[2] + 10, (mid.bbox[1] + mid.bbox[3]) / 2)
        _marquee(view, rect, direction="lr")  # window: only the fully-enclosed top
        assert _selected_texts(view) == [top.text]

        _marquee(view, rect, direction="rl")  # crossing: the straddled mid too
        assert _selected_texts(view) == sorted([top.text, mid.text])
    finally:
        window.close()


def test_rotated_box_is_never_marquee_selected(qapp, tmp_path, monkeypatch):
    path = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "horizontal one", fontsize=9)
    page.insert_text((72, 140), "rotated label", fontsize=9, rotate=90)
    doc.save(str(path))
    doc.close()
    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        view.set_edit_mode(True)
        _set_mods(monkeypatch)
        _marquee(view, (0, 0, 1000, 1000), direction="lr")  # enclose the whole page
        assert all("rotated" not in pp.text for _pn, pp in view._multi_paragraphs)
        assert any("horizontal" in pp.text for _pn, pp in view._multi_paragraphs)
    finally:
        window.close()


def test_shift_marquee_adds_and_ctrl_marquee_removes(qapp, tmp_path, monkeypatch):
    window, view = _view(tmp_path)
    try:
        paras = _paras_by_y(view)
        top, bot = paras[0], paras[2]

        _set_mods(monkeypatch)  # plain: select the top box alone
        _marquee(view, (60, 90, top.bbox[2] + 10, top.bbox[3] + 3), direction="lr")
        assert _selected_texts(view) == [top.text]

        _set_mods(monkeypatch, shift=True)  # add the bottom box
        _marquee(view, (60, bot.bbox[1] - 3, bot.bbox[2] + 10, bot.bbox[3] + 3), direction="lr")
        assert _selected_texts(view) == sorted([top.text, bot.text])

        _set_mods(monkeypatch, ctrl=True)  # remove the top box again
        _marquee(view, (60, 90, top.bbox[2] + 10, top.bbox[3] + 3), direction="lr")
        assert _selected_texts(view) == [bot.text]
    finally:
        window.close()


def test_plain_click_clears_but_modified_click_keeps(qapp, tmp_path, monkeypatch):
    window, view = _view(tmp_path)
    try:
        paras = _paras_by_y(view)
        top = paras[0]
        _set_mods(monkeypatch)
        _marquee(view, (60, 90, top.bbox[2] + 10, top.bbox[3] + 3), direction="lr")
        assert view._multi_paragraphs  # something selected

        # A zero-size marquee (a click) with Shift keeps the selection...
        _set_mods(monkeypatch, shift=True)
        p = _scene(view, 300, 300)
        view._on_box_marquee_finished(p[0], p[1], p[0], p[1])
        assert view._multi_paragraphs

        # ...a plain click clears it.
        _set_mods(monkeypatch)
        view._on_box_marquee_finished(p[0], p[1], p[0], p[1])
        assert not view._multi_paragraphs
    finally:
        window.close()


def test_shift_marquee_does_not_fold_a_hidden_single_selection(qapp, tmp_path, monkeypatch):
    """Review finding: a plain click on a box while a multi-selection is shown
    sets _selection (invisible — multi has chrome priority). A later
    Shift-marquee must NOT silently fold that hidden box into the group."""
    window, view = _view(tmp_path)
    try:
        paras = _paras_by_y(view)
        top, mid, bot = paras[0], paras[1], paras[2]

        # Multi-select top + mid.
        _set_mods(monkeypatch)
        rect = (60, 90, max(top.bbox[2], mid.bbox[2]) + 10, mid.bbox[3] + 5)
        _marquee(view, rect, direction="lr")
        assert len(view._multi_paragraphs) == 2

        # Plain-click the BOTTOM box: sets _selection (hidden behind multi chrome).
        cx = (bot.bbox[0] + bot.bbox[2]) / 2
        cy = (bot.bbox[1] + bot.bbox[3]) / 2
        view._on_select_drag_started(*_scene(view, cx, cy))
        assert view._selection is not None  # hidden single selection exists

        # Shift-marquee a FAR-AWAY empty area (selects nothing): must not pull
        # the hidden bottom box in.
        _set_mods(monkeypatch, shift=True)
        _marquee(view, (500, 500, 520, 520), direction="lr")
        assert not any("row three" in pp.text for _pn, pp in view._multi_paragraphs)
        assert len(view._multi_paragraphs) == 2  # still just top + mid
    finally:
        window.close()


def test_canvas_begin_marquee_emits_on_release(qapp, tmp_path):
    """Wiring: begin_box_marquee + a real release event emits boxMarqueeFinished
    with the press and release scene points."""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    window, view = _view(tmp_path)
    try:
        window.resize(1000, 800)
        window.show()
        qapp.processEvents()
        canvas = view._canvas
        got: list[tuple] = []
        canvas.boxMarqueeFinished.connect(lambda *a: got.append(a))

        press = QPointF(*_scene(view, 60, 90))
        canvas.begin_box_marquee(press.x(), press.y())
        assert canvas._box_marquee_press is not None

        release_scene = QPointF(*_scene(view, 200, 200))
        vp = canvas.mapFromScene(release_scene)
        evt = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(vp),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(evt)
        assert canvas._box_marquee_press is None  # cleared
        assert len(got) == 1
        assert got[0][0] == pytest.approx(press.x(), abs=1.0)
    finally:
        window.close()


def test_marquee_selection_drives_a_group_move(qapp, tmp_path, monkeypatch):
    """The marquee builds an ordinary multi-selection, so a following drag on a
    member moves the whole group (E10.7 path, unchanged)."""
    window, view = _view(tmp_path)
    try:
        _set_mods(monkeypatch)
        paras = _paras_by_y(view)
        top, mid = paras[0], paras[1]
        rect = (60, 90, max(top.bbox[2], mid.bbox[2]) + 10, mid.bbox[3] + 5)
        _marquee(view, rect, direction="lr")
        assert len(view._multi_paragraphs) == 2

        z = view._canvas.render_zoom
        cx = (top.bbox[0] + top.bbox[2]) / 2
        cy = (top.bbox[1] + top.bbox[3]) / 2
        sx, sy = _scene(view, cx, cy)
        view._on_move_drag_started(sx, sy)  # grabs a group member
        assert view._move_group is not None
        before = {s.text.strip(): s.origin for s in view.document.text_spans(0)}
        view._on_move_drag_finished(sx, sy, sx + 40 * z, sy)
        after = {s.text.strip(): s.origin for s in view.document.text_spans(0)}
        # Both column boxes shifted right by ~40 pt.
        assert after["row one alpha"][0] == pytest.approx(before["row one alpha"][0] + 40, abs=1.0)
        assert after["row two beta"][0] == pytest.approx(before["row two beta"][0] + 40, abs=1.0)
    finally:
        window.close()
