"""The first render after a document opens happens before the canvas is laid
out, so fit clamps to the minimum zoom and produces a tiny placeholder image.
When the view then gets its real size, that placeholder must be replaced on the
next event-loop turn — NOT after the 120ms zoom debounce — or it sits blocky and
smoothed on screen for the whole debounce window (the reported "pixelated for a
brief moment"). These tests pin _schedule_rezoom's decision: a fit that would
grossly upscale the current pixmap re-renders immediately; a normal fit keeps
the debounce that coalesces a resize stream."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.page_canvas import _REZOOM_DELAY_MS, PageCanvas  # noqa: E402


def test_placeholder_fit_reschedules_immediately(qapp):
    canvas = PageCanvas()
    # The construction placeholder: rendered at the 0.1 floor before layout,
    # then the real size makes the fit jump to ~1.8 (an ~18x upscale).
    canvas._render_zoom = 0.1
    canvas._zoom = 1.8
    canvas._schedule_rezoom()
    assert canvas._rezoom_timer.isActive()
    # Interval 0 == "next event-loop turn", so the crisp render lands before the
    # blocky placeholder is left on screen for the debounce window.
    assert canvas._rezoom_timer.interval() == 0


def test_normal_fit_keeps_the_debounce(qapp):
    canvas = PageCanvas()
    # A fit between two laid-out sizes (edge-drag / snap / page change) never
    # jumps far enough to look like a placeholder — it must stay debounced so a
    # resize STREAM coalesces into a single re-render.
    canvas._render_zoom = 1.0
    canvas._zoom = 1.4
    canvas._schedule_rezoom()
    assert canvas._rezoom_timer.isActive()
    assert canvas._rezoom_timer.interval() == _REZOOM_DELAY_MS


def test_debounce_restored_after_an_immediate_reschedule(qapp):
    # QTimer.start(msec) permanently resets the interval, so an immediate (0ms)
    # reschedule must not leave later debounced renders firing at 0ms too.
    canvas = PageCanvas()
    canvas._render_zoom = 0.1
    canvas._zoom = 2.0
    canvas._schedule_rezoom()  # placeholder -> 0
    assert canvas._rezoom_timer.interval() == 0
    canvas._render_zoom = 2.0  # crisp render landed; now zoom matches
    canvas._zoom = 2.4
    canvas._schedule_rezoom()  # normal -> debounce again
    assert canvas._rezoom_timer.interval() == _REZOOM_DELAY_MS


def test_manual_zoom_always_debounces(qapp):
    # Ctrl+wheel / zoom buttons must coalesce a rapid stream even when the
    # transient upscale is large — _set_zoom never takes the immediate path.
    canvas = PageCanvas()
    canvas._render_zoom = 0.1
    canvas._set_zoom(2.0)
    assert canvas._rezoom_timer.interval() == _REZOOM_DELAY_MS


# --- dock-resize hang fix: never scale() inside resizeEvent ------------------


def test_resize_defers_the_fit_instead_of_scaling_synchronously(qapp):
    # The field-captured hang was scale() called from _fit_to from resizeEvent
    # while Qt was mid-resize. resizeEvent must now DEFER the fit to a 0-timer,
    # never running it (and its scale()) on the resize stack.
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QPixmap, QResizeEvent

    canvas = PageCanvas()
    canvas.set_page(QPixmap(85, 60), 0.1, (842.0, 595.0))
    called = []
    canvas._fit_to = lambda mode: called.append(mode)  # spy
    canvas.resizeEvent(QResizeEvent(QSize(1200, 900), QSize(800, 600)))
    assert called == []  # NOT fitted synchronously (that is the hang)
    assert canvas._resize_fit_timer.isActive()  # deferred instead


def test_deferred_fit_applies_the_pending_fit(qapp):
    from PySide6.QtGui import QPixmap

    canvas = PageCanvas()
    canvas.set_page(QPixmap(85, 60), 0.1, (842.0, 595.0))
    called = []
    canvas._fit_to = lambda mode: called.append(mode)
    canvas._apply_resize_fit()  # what the 0-timer fires
    assert called == ["page"]


def test_fit_page_forces_scrollbars_off(qapp):
    from PySide6.QtCore import Qt

    canvas = PageCanvas()  # default fit mode is "page"
    off = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    # A fit page never needs scrollbars; forcing them off removes the AsNeeded
    # on/off flip that can oscillate at boundary widths inside scale().
    assert canvas.horizontalScrollBarPolicy() == off
    assert canvas.verticalScrollBarPolicy() == off


def test_free_zoom_restores_asneeded_scrollbars(qapp):
    from PySide6.QtCore import Qt

    canvas = PageCanvas()
    canvas._render_zoom = 1.0
    canvas._set_zoom(2.0)  # leaves fit mode -> free zoom, needs panning
    as_needed = Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert canvas.horizontalScrollBarPolicy() == as_needed
    assert canvas.verticalScrollBarPolicy() == as_needed
