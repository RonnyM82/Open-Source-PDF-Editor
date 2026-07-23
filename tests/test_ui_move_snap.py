"""Offscreen tests for Shift axis-snap on a move drag (task 4).

Holding Shift while moving constrains the drag to its dominant axis
(horizontal OR vertical), so a nudge keeps an existing column/row aligned.
The snap lives in the canvas (``_axis_snapped``, applied in the move-band
preview and again on release before ``moveDragFinished``), and modifiers are
read LIVE — so it also engages when Shift is pressed part way through a drag.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_canvas import PageCanvas  # noqa: E402


def _shift(monkeypatch, on: bool) -> None:
    mod = Qt.KeyboardModifier.ShiftModifier if on else Qt.KeyboardModifier.NoModifier
    monkeypatch.setattr(
        "pdfapp.page_canvas.QApplication.keyboardModifiers", staticmethod(lambda: mod)
    )


def test_axis_snapped_horizontal_dominant(qapp, monkeypatch):
    canvas = PageCanvas()
    canvas._move_press = QPointF(100.0, 100.0)
    _shift(monkeypatch, True)
    snapped = canvas._axis_snapped(QPointF(160.0, 108.0))  # |dx|=60 > |dy|=8
    assert snapped == QPointF(160.0, 100.0)  # y pinned to the press


def test_axis_snapped_vertical_dominant(qapp, monkeypatch):
    canvas = PageCanvas()
    canvas._move_press = QPointF(100.0, 100.0)
    _shift(monkeypatch, True)
    snapped = canvas._axis_snapped(QPointF(108.0, 160.0))  # |dy|=60 > |dx|=8
    assert snapped == QPointF(100.0, 160.0)  # x pinned to the press


def test_axis_snapped_passthrough_without_shift(qapp, monkeypatch):
    canvas = PageCanvas()
    canvas._move_press = QPointF(100.0, 100.0)
    _shift(monkeypatch, False)
    p = QPointF(160.0, 130.0)
    assert canvas._axis_snapped(p) == p  # unconstrained


def test_axis_snapped_passthrough_without_press(qapp, monkeypatch):
    canvas = PageCanvas()
    canvas._move_press = None
    _shift(monkeypatch, True)
    p = QPointF(160.0, 130.0)
    assert canvas._axis_snapped(p, None) == p  # nothing to snap against


def _release_event(canvas, scene_pt: QPointF) -> QMouseEvent:
    vp = canvas.mapFromScene(scene_pt)
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(vp),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ShiftModifier,
    )


def test_release_snaps_the_committed_offset(qapp, quote_pdf, monkeypatch):
    """End-to-end through the real release handler: a Shift release emits an
    axis-snapped current point, so the committed move is single-axis."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        window.resize(1000, 800)
        window.show()
        qapp.processEvents()
        view = window.active_view
        view.set_edit_mode(True)
        canvas = view._canvas

        emitted: list[tuple] = []
        canvas.moveDragFinished.connect(lambda *a: emitted.append(a))

        # Put the canvas into an active MOVE (not resize) directly.
        press_scene = QPointF(200.0, 200.0)
        canvas._move_press = press_scene
        canvas._move_base_rect = QRectF(190.0, 190.0, 40.0, 20.0)
        canvas._resize_anchor = None
        _shift(monkeypatch, True)

        # Release well off-axis (mostly horizontal): dx dominates -> y pins.
        release_scene = QPointF(320.0, 214.0)
        canvas.mouseReleaseEvent(_release_event(canvas, release_scene))

        assert len(emitted) == 1
        px, py, cx, cy = emitted[0]
        assert (px, py) == (200.0, 200.0)
        assert cx == pytest.approx(320.0, abs=1.0)  # x free
        assert cy == pytest.approx(200.0, abs=1.0)  # y snapped to the press
    finally:
        window.close()


def test_release_without_shift_is_unconstrained(qapp, quote_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        window.resize(1000, 800)
        window.show()
        qapp.processEvents()
        view = window.active_view
        view.set_edit_mode(True)
        canvas = view._canvas
        emitted: list[tuple] = []
        canvas.moveDragFinished.connect(lambda *a: emitted.append(a))

        canvas._move_press = QPointF(200.0, 200.0)
        canvas._move_base_rect = QRectF(190.0, 190.0, 40.0, 20.0)
        canvas._resize_anchor = None
        _shift(monkeypatch, False)

        vp = canvas.mapFromScene(QPointF(320.0, 260.0))
        evt = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(vp),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(evt)

        px, py, cx, cy = emitted[0]
        assert cx == pytest.approx(320.0, abs=1.0)
        assert cy == pytest.approx(260.0, abs=1.0)  # both axes free
    finally:
        window.close()
