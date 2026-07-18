"""Offscreen tests for image insert/replace and highlight click modes (E6/E7)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def test_insert_image_click_to_place_and_undo(qapp, quote_pdf, sample_png, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)

        window.insert_image()  # menu action: pick file, arm click-to-place
        assert view._canvas._insert_armed
        z = view._canvas.render_zoom
        view._canvas.insertPointSelected.emit(300 * z, 400 * z)

        assert len(view.document.images(0)) == 2
        assert view.dirty
        view.undo_stack.undo()
        assert len(view.document.images(0)) == 1
        assert not view.dirty
    finally:
        window.close()


def test_insert_image_cancelled_dialog_does_nothing(qapp, quote_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        monkeypatch.setattr(view, "_prompt_image_path", lambda: None)
        window.insert_image()
        assert not view._canvas._insert_armed  # nothing armed without a file
        assert view.undo_stack.count() == 0
    finally:
        window.close()


def test_double_click_image_replaces_it(qapp, quote_pdf, sample_png, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        monkeypatch.setattr(view, "_prompt_image_path", lambda: sample_png)

        logo = view.document.images(0)[0]
        cx = (logo.bbox[0] + logo.bbox[2]) / 2
        cy = (logo.bbox[1] + logo.bbox[3]) / 2
        z = view._canvas.render_zoom
        view._on_point_activated(cx * z, cy * z)  # double-click on the image

        assert len(view.document.images(0)) == 1  # swapped, not added
        assert view.dirty
        view.undo_stack.undo()
        assert not view.dirty
    finally:
        window.close()


def _photo_pdf(tmp_path):
    import pymupdf

    path = tmp_path / "photo.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40))
    pix.clear_with(30)
    page.insert_text((72, 400), "text elsewhere", fontsize=10)
    page.insert_image(pymupdf.Rect(80, 100, 200, 180), pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


def test_ctrl_drag_moves_image_and_undo(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_photo_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        image = view.document.images(0)[0]
        cx = (image.bbox[0] + image.bbox[2]) / 2
        cy = (image.bbox[1] + image.bbox[3]) / 2
        z = view._canvas.render_zoom

        view._on_move_drag_started(cx * z, cy * z)  # Ctrl+press on the image
        assert view._move_image_target is not None
        assert view._move_paragraph is None
        view._on_move_drag_finished(cx * z, cy * z, (cx + 60) * z, (cy + 40) * z)

        moved = view.document.images(0)[0]
        assert moved.bbox[0] == pytest.approx(image.bbox[0] + 60, abs=2.5)
        assert moved.bbox[1] == pytest.approx(image.bbox[1] + 40, abs=2.5)
        assert view.dirty

        view.undo_stack.undo()
        restored = view.document.images(0)[0]
        assert restored.bbox[0] == pytest.approx(image.bbox[0], abs=2.0)
        assert not view.dirty
    finally:
        window.close()


def test_ctrl_drag_corner_resizes_image(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_photo_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        image = view.document.images(0)[0]
        x0, y0, x1, y1 = image.bbox
        z = view._canvas.render_zoom

        # Ctrl+press near the bottom-right corner -> resize (anchor = top-left).
        view._on_move_drag_started(x1 * z, y1 * z)
        assert view._resize_image is not None
        assert view._move_image_target is None
        # Drag the corner inward to roughly half width.
        half_x = x0 + (x1 - x0) / 2
        half_y = y0 + (y1 - y0) / 2
        view._on_move_drag_finished(x1 * z, y1 * z, half_x * z, half_y * z)

        resized = view.document.images(0)[0]
        assert (resized.bbox[2] - resized.bbox[0]) < (x1 - x0) - 5  # shrank
        assert resized.bbox[0] == pytest.approx(x0, abs=2.0)  # top-left anchored
        assert resized.bbox[1] == pytest.approx(y0, abs=2.0)
        assert view.undo_stack.count() == 1

        view.undo_stack.undo()
        assert view.document.images(0)[0].bbox[2] == pytest.approx(x1, abs=2.0)
    finally:
        window.close()


def test_ctrl_drag_image_body_moves_not_resizes(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_photo_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        image = view.document.images(0)[0]
        cx = (image.bbox[0] + image.bbox[2]) / 2
        cy = (image.bbox[1] + image.bbox[3]) / 2
        z = view._canvas.render_zoom
        view._on_move_drag_started(cx * z, cy * z)  # centre -> move
        assert view._move_image_target is not None
        assert view._resize_image is None
    finally:
        window.close()


def test_ctrl_drag_tiny_on_image_is_noop(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_photo_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        image = view.document.images(0)[0]
        cx = (image.bbox[0] + image.bbox[2]) / 2
        cy = (image.bbox[1] + image.bbox[3]) / 2
        z = view._canvas.render_zoom
        view._on_move_drag_started(cx * z, cy * z)
        # 0.4 page-pt of drift (threshold is 1pt in PAGE space, not scene px).
        view._on_move_drag_finished(cx * z, cy * z, (cx + 0.4) * z, (cy + 0.4) * z)
        assert view.undo_stack.count() == 0
    finally:
        window.close()


def test_rotate_image_and_undo(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_photo_pdf(tmp_path))
        view = window.active_view
        image = view.document.images(0)[0]  # 120x80 at (80,100)

        view._rotate_image_at(0, image, -90)  # what context-menu "CW" calls
        rotated = view.document.images(0)[0]
        # Rect swapped about the centre (140, 140): now 80 wide, 120 tall.
        assert (rotated.bbox[2] - rotated.bbox[0]) == pytest.approx(80.0, abs=2.0)
        assert (rotated.bbox[3] - rotated.bbox[1]) == pytest.approx(120.0, abs=2.0)
        assert view.undo_stack.count() == 1
        assert view.dirty

        view.undo_stack.undo()
        restored = view.document.images(0)[0]
        assert (restored.bbox[2] - restored.bbox[0]) == pytest.approx(120.0, abs=2.0)
        assert not view.dirty
    finally:
        window.close()


def test_delete_image_and_undo(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_photo_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        image = view.document.images(0)[0]

        view._delete_image_at(0, image)  # what the context-menu Delete calls
        assert view.document.images(0) == []
        assert view.dirty

        view.undo_stack.undo()
        assert len(view.document.images(0)) == 1
        assert not view.dirty
    finally:
        window.close()


def test_context_menu_offscreen_is_noop(qapp, tmp_path):
    """The right-click menu execs a modal QMenu — offscreen it must not (it
    would hang); the dispatch methods are tested directly instead."""
    window = MainWindow()
    try:
        window.open_path(_photo_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        image = view.document.images(0)[0]
        cx = (image.bbox[0] + image.bbox[2]) / 2
        cy = (image.bbox[1] + image.bbox[3]) / 2
        z = view._canvas.render_zoom
        view._on_context_menu(cx * z, cy * z)  # not visible -> returns immediately
        assert view.undo_stack.count() == 0
    finally:
        window.close()


def test_highlight_window_selection_and_undo(qapp, quote_pdf):
    """E9.1: highlight is a dragged WINDOW — everything inside it, one undo."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        price = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        total = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.total)

        window.highlight_text()
        assert view._canvas._region_armed  # armed as a window selection
        z = view._canvas.render_zoom
        # Drag a window covering both table cells (same row).
        x0 = min(price.bbox[0], total.bbox[0]) - 2
        x1 = max(price.bbox[2], total.bbox[2]) + 2
        y0 = min(price.bbox[1], total.bbox[1]) - 1
        y1 = max(price.bbox[3], total.bbox[3]) + 1
        view._canvas.regionSelected.emit(x0 * z, y0 * z, x1 * z, y1 * z)

        page = view.document._doc[0]  # keep alive: annots orphan otherwise
        assert len(list(page.annots())) >= 2  # both cells highlighted
        assert view.undo_stack.count() == 1  # ... in ONE undo step
        assert view.dirty
        view.undo_stack.undo()
        page = view.document._doc[0]
        assert list(page.annots()) == []
    finally:
        window.close()


def test_highlight_tiny_drag_falls_back_to_span(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)

        window.highlight_text()
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        z = view._canvas.render_zoom
        view._canvas.regionSelected.emit(cx * z, cy * z, cx * z + 0.2, cy * z + 0.2)

        page = view.document._doc[0]
        assert len(list(page.annots())) == 1  # the span under the click
    finally:
        window.close()


def test_armed_press_suppresses_following_double_click(qapp, quote_pdf, sample_png, monkeypatch):
    """Review finding: double-clicking while armed placed the image AND then
    immediately re-prompted to replace it (the dblclick hit the fresh image)."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        prompts: list[int] = []

        def prompt():
            prompts.append(1)
            return sample_png

        monkeypatch.setattr(view, "_prompt_image_path", prompt)
        window.insert_image()

        canvas = view._canvas
        z = canvas.render_zoom
        # Simulate the armed press consuming click #1...
        canvas._insert_armed = True
        canvas._suppress_dblclick = True
        canvas.insertPointSelected.emit(300 * z, 400 * z)
        canvas._insert_armed = False
        # ...and the double-click that follows it must be eaten.
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtCore import QPointF as QPF
        from PySide6.QtGui import QMouseEvent

        dbl = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            QPF(10.0, 10.0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseDoubleClickEvent(dbl)

        assert len(prompts) == 1  # no second "Choose image" prompt
        assert view.undo_stack.count() == 1  # exactly one insert command
    finally:
        window.close()


def test_highlight_empty_window_warns_and_pushes_nothing(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        messages: list[str] = []
        view.editWarning.connect(messages.append)
        window.highlight_text()
        z = view._canvas.render_zoom
        view._canvas.regionSelected.emit(400 * z, 500 * z, 500 * z, 560 * z)  # blank area
        assert any("No text in the selection" in m for m in messages)
        assert view.undo_stack.count() == 0
    finally:
        window.close()
