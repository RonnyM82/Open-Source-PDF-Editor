"""Offscreen tests for plain-scroll page navigation.

An unmodified wheel/trackpad scroll at a page's top/bottom edge crosses to
the previous/next page (landing at the matching edge); mid-page scrolling is
untouched. Discrete mouse notches cross per event; trackpad-style small-delta
streams accumulate to a threshold and are swallowed after a flip until a
quiet gap, so one continuous swipe (plus its momentum tail) moves ONE page.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPixmap, QWheelEvent  # noqa: E402

from pdfapp import page_canvas as page_canvas_module  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_canvas import PageCanvas  # noqa: E402


@pytest.fixture
def scroll_clock(monkeypatch):
    """Fake the canvas's monotonic clock so stream gaps are deterministic."""
    clock = {"t": 0.0}
    monkeypatch.setattr(page_canvas_module, "_now_ms", lambda: clock["t"])
    return clock


def _wheel(
    canvas: PageCanvas,
    *,
    angle: int = 0,
    pixel: int = 0,
    modifiers=Qt.KeyboardModifier.NoModifier,
) -> QWheelEvent:
    phase = Qt.ScrollPhase.ScrollUpdate if pixel else Qt.ScrollPhase.NoScrollPhase
    event = QWheelEvent(
        QPointF(50.0, 50.0),
        QPointF(50.0, 50.0),
        QPoint(0, pixel),
        QPoint(0, angle),
        Qt.MouseButton.NoButton,
        modifiers,
        phase,
        False,
    )
    canvas.wheelEvent(event)
    return event


def _white_pixmap(w: int, h: int) -> QPixmap:
    pixmap = QPixmap(w, h)
    pixmap.fill(QColor("white"))
    return pixmap


def _fit_canvas() -> PageCanvas:
    """A canvas whose page fully fits the viewport: no scroll range at all."""
    canvas = PageCanvas()
    canvas.resize(400, 500)
    canvas.set_page(_white_pixmap(200, 300), 1.0, (200.0, 300.0))
    assert canvas.verticalScrollBar().maximum() == 0  # precondition
    return canvas


def _zoomed_canvas() -> PageCanvas:
    """A canvas zoomed until the page is taller than the viewport."""
    canvas = _fit_canvas()
    for _ in range(6):
        canvas.zoom_in()
    # Precondition: a real scroll range, well past the fully-visible slack.
    assert canvas.verticalScrollBar().maximum() > page_canvas_module._EDGE_FIT_SLACK_PX
    return canvas


def _spy(canvas: PageCanvas) -> list[int]:
    requests: list[int] = []
    canvas.pageScrollRequested.connect(requests.append)
    return requests


# --- canvas: edge detection and stream handling ------------------------------


def test_mid_page_scroll_scrolls_within_page(qapp):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue((sb.minimum() + sb.maximum()) // 2)

    before = sb.value()
    _wheel(canvas, angle=-120)
    assert sb.value() > before  # normal scrolling, exactly as today
    assert requests == []

    before = sb.value()
    _wheel(canvas, angle=120)
    assert sb.value() < before
    assert requests == []


def test_notch_at_bottom_requests_next_page(qapp):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue(sb.maximum())

    event = _wheel(canvas, angle=-120)
    assert requests == [1]
    assert event.isAccepted()
    assert sb.value() == sb.maximum()  # the crossing scroll never also scrolls


def test_notch_at_top_requests_previous_page(qapp):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    canvas.verticalScrollBar().setValue(canvas.verticalScrollBar().minimum())

    _wheel(canvas, angle=120)
    assert requests == [-1]


def test_inward_scroll_at_an_edge_scrolls_normally(qapp):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue(sb.maximum())

    _wheel(canvas, angle=120)  # scroll UP while at the bottom: just scroll
    assert sb.value() < sb.maximum()
    assert requests == []


def test_fully_visible_page_notch_flips_directly(qapp):
    canvas = _fit_canvas()
    requests = _spy(canvas)

    _wheel(canvas, angle=-120)
    _wheel(canvas, angle=120)
    assert requests == [1, -1]


def test_trackpad_burst_advances_one_page_then_holds(qapp, scroll_clock):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue(sb.maximum())

    # One continuous two-finger swipe: many small pixel-delta events.
    for _ in range(10):
        scroll_clock["t"] += 10.0
        _wheel(canvas, pixel=-20, angle=-30)
    assert requests == [1]

    # The momentum tail after the fingers lift: smaller, sparser, same stream.
    for delta in (-14, -9, -5, -2, -1):
        scroll_clock["t"] += 40.0
        _wheel(canvas, pixel=delta, angle=delta)
    assert requests == [1]
    assert sb.value() == sb.maximum()  # swallowed events never scrolled either


def test_second_swipe_after_a_pause_flips_again(qapp, scroll_clock):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue(sb.maximum())

    for _ in range(5):
        scroll_clock["t"] += 10.0
        _wheel(canvas, pixel=-20, angle=-30)
    assert requests == [1]

    scroll_clock["t"] += 1000.0  # fingers lifted, momentum long gone
    for _ in range(5):
        scroll_clock["t"] += 10.0
        _wheel(canvas, pixel=-20, angle=-30)
    assert requests == [1, 1]


def test_tiny_nudges_never_flip(qapp, scroll_clock):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue(sb.maximum())

    scroll_clock["t"] += 10.0
    _wheel(canvas, pixel=-20, angle=-30)  # one sub-threshold push
    assert requests == []

    scroll_clock["t"] += 1000.0
    _wheel(canvas, pixel=-20, angle=-30)  # much later: must not combine
    assert requests == []


def test_sub_notch_wheel_stream_accumulates_to_one_notch(qapp, scroll_clock):
    # High-resolution mouse wheels report fractional notches (angle < 120,
    # no pixelDelta) — treated as a stream, not as per-event notches.
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue(sb.maximum())

    for _ in range(2):
        scroll_clock["t"] += 20.0
        _wheel(canvas, angle=-40)
    assert requests == []  # 80 of 120 accumulated

    scroll_clock["t"] += 20.0
    _wheel(canvas, angle=-40)
    assert requests == [1]

    for _ in range(6):  # the wheel keeps coasting: held, no further flips
        scroll_clock["t"] += 20.0
        _wheel(canvas, angle=-40)
    assert requests == [1]


def test_modified_wheel_never_flips_pages(qapp):
    canvas = _zoomed_canvas()
    requests = _spy(canvas)
    sb = canvas.verticalScrollBar()
    sb.setValue(sb.maximum())

    before = canvas.zoom
    _wheel(canvas, angle=-120, modifiers=Qt.KeyboardModifier.ControlModifier)
    assert canvas.zoom < before  # Ctrl+wheel still zooms, exactly as before
    assert requests == []

    sb.setValue(sb.maximum())
    _wheel(canvas, angle=-120, modifiers=Qt.KeyboardModifier.ShiftModifier)
    assert requests == []  # only PLAIN scrolling navigates


# --- view integration: the flip goes through normal navigation ---------------


def _open_scrollable(window: MainWindow, path):
    window.open_path(path)
    view = window.active_view
    canvas = view._canvas
    sb = canvas.verticalScrollBar()
    for _ in range(12):
        if sb.maximum() > page_canvas_module._EDGE_FIT_SLACK_PX:
            break
        canvas.zoom_in()
    # Zoomed: the page no longer fits, well past the fully-visible slack.
    assert sb.maximum() > page_canvas_module._EDGE_FIT_SLACK_PX
    return view, canvas, sb


def test_wheel_flip_lands_at_top_and_syncs_chrome(qapp, multipage_pdf):
    window = MainWindow()
    try:
        view, canvas, sb = _open_scrollable(window, multipage_pdf)
        sb.setValue(sb.maximum())

        _wheel(canvas, angle=-120)
        assert view.current_page == 1
        assert sb.value() == sb.minimum()  # landed at the true top
        assert window._page_spin.value() == 2  # page indicator followed
    finally:
        window.close()


def test_wheel_flip_up_lands_at_bottom(qapp, multipage_pdf):
    window = MainWindow()
    try:
        view, canvas, sb = _open_scrollable(window, multipage_pdf)
        view.go_to_page(1)
        sb.setValue(sb.minimum())

        _wheel(canvas, angle=120)
        assert view.current_page == 0
        assert sb.maximum() > 0  # still zoomed: "bottom" is a real position
        assert sb.value() == sb.maximum()  # landed at the true bottom
    finally:
        window.close()


def test_no_wrap_at_document_bounds(qapp, multipage_pdf):
    window = MainWindow()
    try:
        view, canvas, sb = _open_scrollable(window, multipage_pdf)
        sb.setValue(sb.minimum())
        _wheel(canvas, angle=120)  # scroll up at the very first page's top
        assert view.current_page == 0

        view.last_page()
        sb.setValue(sb.maximum())
        _wheel(canvas, angle=-120)  # scroll down at the very last page's bottom
        assert view.current_page == 4
    finally:
        window.close()


def test_fit_page_wheel_walks_pages(qapp, multipage_pdf):
    window = MainWindow()
    try:
        # A real layout pass (offscreen show) so fit-page truly fits: an
        # unshown window's viewport is degenerate and the fit zoom clamps.
        window.resize(900, 700)
        window.show()
        window.open_path(multipage_pdf)
        view = window.active_view
        canvas = view._canvas
        sb = canvas.verticalScrollBar()
        # Fit-page in a real window keeps a few px of frame/rounding slack —
        # within the slack the canvas treats the page as fully visible.
        assert sb.maximum() - sb.minimum() <= page_canvas_module._EDGE_FIT_SLACK_PX

        _wheel(canvas, angle=-120)
        _wheel(canvas, angle=-120)
        assert view.current_page == 2
        _wheel(canvas, angle=120)
        assert view.current_page == 1
    finally:
        window.close()


def test_trackpad_burst_flips_exactly_one_page(qapp, multipage_pdf, scroll_clock):
    window = MainWindow()
    try:
        window.resize(900, 700)
        window.show()  # real layout so the fit-page state is genuine
        window.open_path(multipage_pdf)
        view = window.active_view
        canvas = view._canvas
        sb = canvas.verticalScrollBar()
        assert sb.maximum() - sb.minimum() <= page_canvas_module._EDGE_FIT_SLACK_PX

        for _ in range(8):  # one continuous swipe on a fully-visible page
            scroll_clock["t"] += 10.0
            _wheel(canvas, pixel=-25, angle=-40)
        assert view.current_page == 1

        for delta in (-20, -15, -10, -6, -3, -1):  # its momentum tail
            scroll_clock["t"] += 25.0
            _wheel(canvas, pixel=delta, angle=delta)
        assert view.current_page == 1
    finally:
        window.close()
