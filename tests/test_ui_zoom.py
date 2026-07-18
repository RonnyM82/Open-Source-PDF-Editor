"""Offscreen tests for zoom behavior (M4 + resolution-matched rendering)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402

from pdfapp.page_canvas import PageCanvas  # noqa: E402


def _white_pixmap(w: int, h: int) -> QPixmap:
    pixmap = QPixmap(w, h)
    pixmap.fill(QColor("white"))
    return pixmap


def _canvas_with_page() -> PageCanvas:
    canvas = PageCanvas()
    canvas.resize(400, 500)
    # A 200x300pt page rendered at engine zoom 1.0.
    canvas.set_page(_white_pixmap(200, 300), 1.0, (200.0, 300.0))
    return canvas


# --- canvas zoom ------------------------------------------------------------


def test_zoom_in_then_out_changes_scale(qapp):
    canvas = _canvas_with_page()
    canvas.fit_page()
    assert canvas.fit_mode == "page"

    base = canvas.transform().m11()
    canvas.zoom_in()
    assert canvas.fit_mode is None
    zoomed_in = canvas.transform().m11()
    assert zoomed_in > base

    canvas.zoom_out()
    assert canvas.transform().m11() < zoomed_in


def test_fit_modes_set_mode_and_zoom(qapp):
    canvas = _canvas_with_page()
    canvas.fit_width()
    assert canvas.fit_mode == "width"
    assert canvas.zoom > 0
    canvas.fit_page()
    assert canvas.fit_mode == "page"


def test_free_zoom_persists_across_page_swap(qapp):
    canvas = _canvas_with_page()
    canvas.zoom_in()
    canvas.zoom_in()
    zoom = canvas.zoom

    canvas.set_page(_white_pixmap(200, 300), 1.0, (200.0, 300.0))
    assert canvas.fit_mode is None
    assert abs(canvas.zoom - zoom) < 1e-9


def test_zoom_change_requests_crisp_render(qapp):
    canvas = _canvas_with_page()
    requested: list[float] = []
    canvas.renderNeeded.connect(requested.append)

    canvas.zoom_in()
    canvas.zoom_in()
    canvas.flush_pending_render()

    assert requested == [canvas.zoom]


def test_dpr_change_requests_rerender(qapp):
    from PySide6.QtCore import QEvent

    canvas = _canvas_with_page()
    canvas.zoom_in()  # free zoom: no resize-driven healing on DPI change
    canvas.flush_pending_render()  # settle

    requested: list[float] = []
    canvas.renderNeeded.connect(requested.append)
    # Simulate the window landing on a monitor with a different scale factor.
    canvas.event(QEvent(QEvent.Type.DevicePixelRatioChange))
    canvas.flush_pending_render()
    assert requested == [canvas.zoom]


def test_update_pixmap_keeps_logical_zoom(qapp):
    canvas = _canvas_with_page()
    canvas.zoom_in()
    zoom = canvas.zoom

    # The crisp re-render arrives at exactly this zoom: the logical zoom is
    # unchanged and the view maps render pixels 1:1 (no resampling at rest).
    canvas.update_pixmap(_white_pixmap(int(200 * zoom), int(300 * zoom)), zoom)
    assert abs(canvas.zoom - zoom) < 1e-9
    assert abs(canvas.transform().m11() - 1.0) < 1e-9


# --- window integration -----------------------------------------------------


def test_zoom_actions_enabled_after_open(qapp, text_pdf):
    from pdfapp.main_window import MainWindow

    window = MainWindow()
    try:
        assert not window._zoom_in_action.isEnabled()  # disabled with no doc
        window.open_path(text_pdf)
        for action in window._zoom_actions:
            assert action.isEnabled()
    finally:
        window.close()


def test_open_renders_at_exact_screen_resolution(qapp, text_pdf):
    from pdfapp.main_window import MainWindow

    window = MainWindow()
    try:
        window.open_path(text_pdf)
        canvas = window.active_view._canvas
        # Offscreen DPR == 1, so the render zoom equals the logical zoom and the
        # view shows it 1:1 — no resampling at rest.
        assert abs(canvas.render_zoom - canvas.zoom) < 1e-9
        assert abs(canvas.transform().m11() - 1.0) < 1e-9
    finally:
        window.close()


def test_zoom_in_rerenders_at_exact_resolution(qapp, text_pdf):
    from pdfapp.main_window import MainWindow

    window = MainWindow()
    try:
        window.open_path(text_pdf)
        view = window.active_view
        canvas = view._canvas
        initial = canvas.render_zoom

        for _ in range(3):
            window.zoom_in()
        canvas.flush_pending_render()

        assert canvas.render_zoom > initial
        # Exact match: render pixels map 1:1 to device pixels (DPR == 1).
        assert abs(canvas.render_zoom - canvas.zoom) < 1e-9
        assert abs(canvas.transform().m11() - 1.0) < 1e-9
        # The exact-zoom render went through the cache.
        assert (0, "main", canvas.render_zoom) in view._cache
    finally:
        window.close()
