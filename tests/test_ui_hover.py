"""Offscreen tests for hover affordances (U2a).

The canvas emits hoverMoved (scene px) on tracked mouse moves; the view
hit-tests the cached geometry synchronously and drives set_hover() /
clear_hover(). Hover is drawn by drawForeground and shown by a per-kind
cursor. Inert in read-only mode (U0 cross-cutting rule).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

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


def test_hover_inert_in_read_only(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        assert not view.edit_mode
        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._canvas._hover_kind is None
    finally:
        window.close()


def test_hover_over_text_outlines_paragraph_with_ibeam(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        px, py = _span_center(view, quote_pdf.price)
        view._on_hover_moved(*_scene_point(view, px, py))
        assert view._canvas._hover_kind == "text"
        assert view._canvas._hover_rect is not None
        cursor = view._canvas.viewport().cursor().shape()
        assert cursor == Qt.CursorShape.IBeamCursor
        # The outline is the PARAGRAPH box, converted to scene px.
        para = view.document.paragraph_at(0, px, py)
        z = view._canvas.render_zoom
        assert view._canvas._hover_rect.left() == pytest.approx(para.bbox[0] * z, abs=0.01)
        assert view._canvas._hover_rect.bottom() == pytest.approx(para.bbox[3] * z, abs=0.01)
    finally:
        window.close()


def test_hover_over_image_body_and_corner_cursors(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        logo = view.document.images(0)[0]
        cx = (logo.bbox[0] + logo.bbox[2]) / 2
        cy = (logo.bbox[1] + logo.bbox[3]) / 2
        view._on_hover_moved(*_scene_point(view, cx, cy))
        assert view._canvas._hover_kind == "image"
        assert view._canvas.viewport().cursor().shape() == Qt.CursorShape.OpenHandCursor

        view._on_hover_moved(*_scene_point(view, logo.bbox[0] + 2, logo.bbox[1] + 2))
        assert view._canvas._hover_kind == "image_corner"
        assert view._canvas.viewport().cursor().shape() in (
            Qt.CursorShape.SizeFDiagCursor,
            Qt.CursorShape.SizeBDiagCursor,
        )
    finally:
        window.close()


def test_hover_clears_on_empty_area_and_mode_exit(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._canvas._hover_kind == "text"

        view._on_hover_moved(*_scene_point(view, 10.0, 10.0))  # no content there
        assert view._canvas._hover_kind is None

        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._canvas._hover_kind == "text"
        view.set_edit_mode(False)  # leaving edit mode drops the affordance
        assert view._canvas._hover_kind is None
    finally:
        window.close()


def test_hover_hit_test_is_rotation_safe(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        window.rotate_clockwise()
        assert view.document.page_rotation(0) == 90
        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._canvas._hover_kind == "text"
    finally:
        window.close()


def test_ctrl_flips_hover_cursor_to_move(qapp, quote_pdf, monkeypatch):
    """The Ctrl move-cursor variant reads LIVE modifier state per move —
    no tracked key state that could stick after a focus loss (U2b)."""
    from PySide6.QtWidgets import QApplication

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        text_point = _scene_point(view, *_span_center(view, quote_pdf.price))

        view._on_hover_moved(*text_point)
        assert view._canvas.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor

        monkeypatch.setattr(
            QApplication,
            "keyboardModifiers",
            staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
        )
        view._on_hover_moved(*text_point)  # same element, Ctrl now held
        assert view._canvas.viewport().cursor().shape() == Qt.CursorShape.SizeAllCursor

        logo = view.document.images(0)[0]
        cx = (logo.bbox[0] + logo.bbox[2]) / 2
        cy = (logo.bbox[1] + logo.bbox[3]) / 2
        view._on_hover_moved(*_scene_point(view, cx, cy))
        assert view._canvas.viewport().cursor().shape() == Qt.CursorShape.SizeAllCursor

        # A corner keeps its diagonal resize cursor with or without Ctrl.
        view._on_hover_moved(*_scene_point(view, logo.bbox[0] + 2, logo.bbox[1] + 2))
        assert view._canvas.viewport().cursor().shape() in (
            Qt.CursorShape.SizeFDiagCursor,
            Qt.CursorShape.SizeBDiagCursor,
        )

        monkeypatch.setattr(
            QApplication,
            "keyboardModifiers",
            staticmethod(lambda: Qt.KeyboardModifier.NoModifier),
        )
        view._on_hover_moved(*text_point)  # Ctrl released
        assert view._canvas.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
    finally:
        window.close()


def test_hover_hint_shows_and_clears_in_status_bar(qapp, quote_pdf):
    from pdfapp.gestures import hover_hint

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)

        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert window.statusBar().currentMessage() == hover_hint("text")
        assert "double-click edits" in hover_hint("text")

        logo = view.document.images(0)[0]
        cx = (logo.bbox[0] + logo.bbox[2]) / 2
        cy = (logo.bbox[1] + logo.bbox[3]) / 2
        view._on_hover_moved(*_scene_point(view, cx, cy))
        assert window.statusBar().currentMessage() == hover_hint("image")

        view._on_hover_moved(*_scene_point(view, 10.0, 10.0))  # empty area
        assert window.statusBar().currentMessage() == ""
    finally:
        window.close()


def test_hover_hint_clear_never_eats_a_warning(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        # A warning lands while the hint is up (8s timeout message).
        window.statusBar().showMessage("Font can't be matched exactly", 8000)

        view._on_hover_moved(*_scene_point(view, 10.0, 10.0))  # hover ends
        assert window.statusBar().currentMessage() == "Font can't be matched exactly"
    finally:
        window.close()


def test_no_hover_hint_in_read_only(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        window.statusBar().clearMessage()  # drop the startup message
        view = window.active_view
        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert window.statusBar().currentMessage() == ""
    finally:
        window.close()


def test_hover_paint_path_renders(qapp, quote_pdf):
    """grab() forces a real paint, executing drawForeground with hover set
    (text outline; image outline + corner ticks) — a paint-time crash or
    QPainter misuse fails here instead of only in the running app."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._canvas._hover_kind == "text"
        assert not view._canvas.grab().isNull()

        logo = view.document.images(0)[0]
        view._on_hover_moved(*_scene_point(view, logo.bbox[0] + 2, logo.bbox[1] + 2))
        assert view._canvas._hover_kind == "image_corner"
        assert not view._canvas.grab().isNull()
    finally:
        window.close()


def test_arming_a_mode_clears_hover_and_keeps_crosshair(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view._on_hover_moved(*_scene_point(view, *_span_center(view, quote_pdf.price)))
        assert view._canvas._hover_kind == "text"
        view.begin_insert_text()
        assert view._canvas._hover_kind is None
        assert view._canvas.viewport().cursor().shape() == Qt.CursorShape.CrossCursor
    finally:
        window.close()
