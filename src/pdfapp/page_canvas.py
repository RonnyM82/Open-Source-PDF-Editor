"""QGraphicsView-based page canvas with resolution-exact rendering.

The canvas tracks a *logical zoom* — logical pixels per PDF point (1.0 == 72
dpi). Instead of scaling one fixed-resolution render (which blurs at high zoom
and on scaled Windows displays), it emits ``renderNeeded(zoom)`` so the window
can supply a pixmap rendered at *exactly* the on-screen resolution
(``zoom × devicePixelRatio``, capped). At rest the view transform times the
device-pixel ratio is ~1.0, so render pixels map 1:1 to device pixels with no
resampling — resampling (especially fractional *downscaling* of small hinted
glyphs) is what makes text look mushy next to Acrobat.

While a crisp render is pending (e.g. during rapid Ctrl+wheel), the current
pixmap is transform-scaled as an instant preview; the exact re-render lands
after a short debounce. The view transform is always ``zoom / render_zoom``.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QRubberBand,
)

from pdfapp import theme

_ZOOM_STEP = 1.25
_MIN_ZOOM = 0.1  # logical px per PDF point
_MAX_ZOOM = 16.0
_REZOOM_DELAY_MS = 120
# A fit that would upscale the current pixmap by more than this factor is
# showing the pre-layout PLACEHOLDER — the page is first rendered from
# DocumentView.__init__ before the canvas has a real size, so fit clamps to
# _MIN_ZOOM (0.1) and produces a tiny image. When the view then gets its real
# size the fit jumps to the true zoom, leaving that tiny image transform-scaled
# ~18x (blocky, smoothed) until the debounced re-render lands. Skip the debounce
# in that one case and re-render on the very next event-loop turn, so the blocky
# placeholder is never left on screen. Real fit transitions between laid-out
# sizes (edge-drag, snap, page change) never jump this far, so they keep the
# debounce that coalesces a resize stream. Self-limiting: once the crisp render
# lands, render_zoom matches the fit and the factor drops back below 1.
_PLACEHOLDER_UPSCALE = 3.0

# Plain-scroll page navigation. A crossing at a page edge must be deliberate:
# a discrete mouse notch qualifies by itself; trackpads (and free-spinning
# high-res wheels) send bursts of small deltas instead, so those accumulate to
# a threshold, and after a flip the rest of the stream — including the
# momentum tail after the fingers lift — is swallowed until a quiet gap.
_EDGE_REARM_MS = 300.0  # inter-event gap that ends a scroll stream
_EDGE_THRESHOLD_PX = 60.0  # deliberate-crossing distance, pixel-delta streams
_EDGE_THRESHOLD_ANGLE = 120.0  # same for sub-notch angle streams (one notch)
# A fit-page view is never EXACTLY range-0 in a real window (frame + pixmap
# rounding leave a few px of slack); a range this small counts as fully
# visible, so a scroll flips instead of nibbling invisible pixels first.
_EDGE_FIT_SLACK_PX = 8
# Hyperlink hotspot chrome — a teal distinct from the accent-blue reveal/hover
# so link zones read as their own thing in both themes over the white page.
_LINK_COLOR = QColor(0, 150, 136)


class PageCanvas(QGraphicsView):
    renderNeeded = Signal(float)  # desired logical zoom (1.0 == 72 dpi)
    # Scene px of a double-click on the page; the bool is True for a
    # paragraph/block edit (Ctrl held), False for a single-span edit.
    pointActivated = Signal(float, float, bool)
    # Scene px chosen after arm_insert_point() (click-to-place new text).
    insertPointSelected = Signal(float, float)
    # Ctrl+drag to move a paragraph. Started fires on Ctrl+press; the view
    # hit-tests and calls begin_move_feedback(scene_rect) to accept the drag
    # (no call = nothing under the cursor, the press is ignored). Finished
    # carries press -> release scene positions.
    moveDragStarted = Signal(float, float)
    moveDragFinished = Signal(float, float, float, float)
    # A plain left press on the page background (not on an open editor, which
    # is a child of the viewport and swallows its own clicks) — commits any
    # open in-place editor ("click away to apply").
    backgroundPressed = Signal()
    # Armed window selection (highlighting): press-drag-release draws a
    # rubber band; fires with the press -> release scene positions.
    regionSelected = Signal(float, float, float, float)
    # Armed link-rectangle draw (create a hyperlink): press-drag-release draws a
    # rubber band; fires with the press -> release scene positions. One-shot
    # (mirrors arm_insert_point), unlike the sticky highlighter region.
    linkRectSelected = Signal(float, float, float, float)
    # Armed signature-rectangle draw (place a digital signature): the same
    # one-shot press-drag-release rubber band as linkRectSelected, its own
    # signal so the sign flow never collides with link creation.
    signRectSelected = Signal(float, float, float, float)
    # The merged Hyperlink tool. On press the view hit-tests and accepts either
    # a TEXT-FLOW drag (begin_link_flow) or a RECTANGLE drag (begin_link_rect),
    # the same synchronous accept protocol as moveDragStarted. While a flow drag
    # is live the canvas reports the cursor so the view can extend + paint the
    # selection; the release carries press+release points and the consecutive-
    # click count (1/2 = word, 3+ = sentence).
    linkDragStarted = Signal(float, float)
    linkDragMoved = Signal(float, float)
    linkDragFinished = Signal(float, float, float, float, int)
    # Right-click on the page (scene px) — the view offers a context menu.
    contextMenuRequested = Signal(float, float)
    # Cursor moved over the page with no buttons and no armed mode (U2a).
    # Synchronous: the view hit-tests and calls set_hover()/clear_hover()
    # before this returns (same pattern as moveDragStarted).
    hoverMoved = Signal(float, float)
    # The hovered element changed ("" = nothing hovered). The view turns the
    # kind into a point-of-need status-bar hint (U2b).
    hoverKindChanged = Signal(str)
    # Plain left press on the page, no Ctrl, no armed mode (U6). Same accept
    # protocol as moveDragStarted: the view selects/deselects, and for a
    # press on the ALREADY-selected element calls begin_move_feedback /
    # begin_resize_feedback to accept a drag (no call = plain click).
    selectDragStarted = Signal(float, float)
    # Esc on the canvas — the view deselects (U6) / disarms (U4).
    escapePressed = Signal()
    # Delete/Backspace on the canvas — the view deletes a selected image.
    deleteSelectionRequested = Signal()
    # An armed one-shot mode started or ended (U4) — chrome re-syncs the
    # launching action's checked state.
    armedChanged = Signal()
    # A plain (unmodified) scroll crossed a page edge: +1 = next page (arrived
    # at the bottom), -1 = previous (arrived at the top). The view flips via
    # the normal navigation path and lands the hand-off with
    # scroll_to_vertical_edge(); at the document bounds it does nothing.
    pageScrollRequested = Signal(int)
    # Read-only flow text selection (X4). A plain press emits selectDragStarted
    # like edit mode; the view accepts it by calling begin_text_selection(), and
    # the canvas then reports live drag positions and the release here. The view
    # owns the selection (page space); the canvas only draws the rects.
    textSelectMoved = Signal(float, float)
    textSelectFinished = Signal(float, float)
    # Edit-mode box marquee (task 1). A plain press on EMPTY page area emits
    # selectDragStarted; the view accepts it by calling begin_box_marquee(), and
    # the canvas draws the rubber band and reports the release rectangle here.
    # Direction (window vs crossing) and modifier (replace/add/remove) are the
    # view's business — the canvas only reports press+release scene points.
    boxMarqueeFinished = Signal(float, float, float, float)
    # Ctrl+C on the focused canvas — the view copies the read-only selection.
    copyRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item: QGraphicsPixmapItem | None = None
        # "page" | "width" | None (None == free/manual zoom)
        self._fit_mode: str | None = "page"
        self._zoom = 1.0  # logical px per PDF point
        self._render_zoom = 1.0  # engine zoom of the current pixmap
        self._page_size_pts: tuple[float, float] | None = None

        self._rezoom_timer = QTimer(self)
        self._rezoom_timer.setSingleShot(True)
        self._rezoom_timer.setInterval(_REZOOM_DELAY_MS)
        self._rezoom_timer.timeout.connect(self._emit_render_needed)

        # Resize refits are DEFERRED through this 0-timer instead of running in
        # resizeEvent: calling QGraphicsView.scale() while Qt is mid-resize
        # (Windows dispatches the resize from its interactive dock/drag modal
        # loop) can wedge the viewport relayout — the field-captured dock hang
        # (page_canvas.scale in _apply_transform in resizeEvent). Coalesces a
        # resize stream into one fit for free.
        self._resize_fit_timer = QTimer(self)
        self._resize_fit_timer.setSingleShot(True)
        self._resize_fit_timer.setInterval(0)
        self._resize_fit_timer.timeout.connect(self._apply_resize_fit)

        # Plain-scroll page navigation state (see _wheel_page_scroll).
        self._scroll_accum = 0.0  # small-delta build-up toward an edge crossing
        self._scroll_accum_kind: str | None = None  # "px" | "angle"
        self._scroll_last_ms = float("-inf")  # last plain-scroll event time
        self._scroll_hold = False  # swallowing the stream after a page flip

        self._insert_armed = False  # one-shot click-to-place for new text
        self._region_armed = False  # window selection (highlight)
        self._region_sticky = False  # highlight tool STAYS armed after each mark
        self._linkrect_armed = False  # one-shot draw-a-rectangle for a new link
        self._linkrect_press = None  # scene QPointF while a link-rect drag is live
        self._signrect_armed = False  # one-shot draw-a-rectangle for a signature
        self._signrect_press = None  # scene QPointF while a sign-rect drag is live
        self._link_hover = False  # read-only pointing-hand cursor over a link
        # The merged Hyperlink tool: STICKY (a click sequence must be able to
        # reach 2 and 3 clicks — a one-shot disarm on the first release is what
        # made triple-click impossible in the first cut).
        self._link_armed = False
        self._link_press = None  # scene QPointF while a link drag is live
        self._link_rect_mode = False  # True = rubber-band rect, False = text flow
        self._link_accepted = False  # the view took the press this cycle
        self._link_clicks = 0  # consecutive-click counter (word / sentence)
        self._link_click_ms = float("-inf")
        self._link_click_scene: QPointF | None = None
        # Consecutive-click counter for the highlight tool (1/2 = word, 3 = line).
        self._region_click_count = 0
        self._region_click_ms = float("-inf")
        self._region_click_scene: QPointF | None = None
        self._region_press = None  # scene QPointF while a region drag is live
        self._text_select_press = None  # scene QPointF while a text drag is live (X4)
        self._box_marquee_press = None  # scene QPointF while a box marquee is live (task 1)
        self._text_hover = False  # read-only I-beam cursor is showing (X4)
        self._suppress_dblclick = False  # armed press consumed -> eat the dblclick
        self._move_press = None  # scene QPointF while a Ctrl+drag is live
        self._move_base_rect: QRectF | None = None  # paragraph rect (scene px)
        self._resize_anchor: QPointF | None = None  # fixed corner during a resize
        self._move_band: QRubberBand | None = None

        # Hover affordance state (U2a): what to outline, in scene px. Drawn
        # by drawForeground so it survives the per-render scene.clear().
        self._hover_kind: str | None = None
        self._hover_rect: QRectF | None = None
        self._hover_corner: QPointF | None = None
        self._hover_zone = 0.0  # corner tick length, scene px
        self.viewport().setMouseTracking(True)
        # Selection chrome (U6): border + (images) corner handles, scene px.
        # The VIEW owns the selection state; this is display only.
        self._selection_kind: str | None = None
        self._selection_rect: QRectF | None = None
        self._multi_selection_rects: list[QRectF] = []  # ctrl/shift multi (E10.7)
        # Reveal-all outlines (U5): every editable area, faint dashed.
        self._reveal_rects: list[QRectF] = []
        # Hyperlink hotspots: every link on the page, in a DISTINCT colour so
        # they read differently from the editable-area reveal (scene px).
        self._link_rects: list[QRectF] = []
        # Search highlights (SR2): all hits + the current hit, scene px.
        # Display only — the view owns the hit list and the current index.
        self._search_rects: list[QRectF] = []
        self._search_current: list[QRectF] = []
        # Read-only text-selection highlight (X4): per-line rects, scene px.
        # Display only — the view owns the selection (page space).
        self._text_selection_rects: list[QRectF] = []
        # Armed-mode chip (U4): a persistent floating hint while a one-shot
        # mode is live — the transient status message was easy to miss.
        self._armed_chip = QLabel(self.viewport())
        self._armed_chip.setObjectName("armed_chip")
        self._armed_chip.hide()

        self.setBackgroundBrush(theme.canvas_brush())
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        # QGraphicsView enables acceptDrops by default and would swallow a
        # file drop over the open page (forwarding it to the scene, which
        # refuses it) — so it never reaches MainWindow's drag-to-open. The
        # canvas uses no Qt drag-and-drop of its own (move/resize are mouse
        # events), so opt out and let file drops propagate up to the window.
        self.setAcceptDrops(False)
        # Default fit mode is "page" -> no scrollbars (see _apply_scrollbar_policy);
        # set it up front so even the first (pre-resize) render is oscillation-safe.
        self._apply_scrollbar_policy()

    @property
    def fit_mode(self) -> str | None:
        return self._fit_mode

    @property
    def has_page(self) -> bool:
        return self._item is not None

    @property
    def zoom(self) -> float:
        """Current logical zoom (logical px per PDF point)."""
        return self._zoom

    @property
    def render_zoom(self) -> float:
        """Engine zoom the displayed pixmap was rendered at."""
        return self._render_zoom

    # --- content --------------------------------------------------------
    def set_page(
        self,
        pixmap: QPixmap,
        render_zoom: float,
        page_size_pts: tuple[float, float],
    ) -> None:
        """Show a page. In a fit mode the zoom is recomputed for this page's
        size; in free zoom the logical zoom persists across pages."""
        self._page_size_pts = page_size_pts
        self._render_zoom = render_zoom
        self._scene.clear()
        self._item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._item.boundingRect())
        if self._fit_mode is not None:
            self._zoom = self._fit_zoom_for(page_size_pts, self._fit_mode)
        self._apply_transform()
        self._refresh_hover()

    def update_pixmap(self, pixmap: QPixmap, render_zoom: float) -> None:
        """Swap in a re-render of the same page (crisper zoom level), keeping
        the visible position."""
        if self._item is None:
            return
        old_center = self.mapToScene(self.viewport().rect().center())
        ratio = render_zoom / self._render_zoom
        self._render_zoom = render_zoom
        self._scene.clear()
        self._item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._item.boundingRect())
        self._apply_transform()
        self.centerOn(old_center.x() * ratio, old_center.y() * ratio)
        self._refresh_hover()

    def clear(self) -> None:
        self._scene.clear()
        self._item = None
        self._page_size_pts = None
        self.clear_hover()

    # --- zoom -----------------------------------------------------------
    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * _ZOOM_STEP)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / _ZOOM_STEP)

    def fit_page(self) -> None:
        self._fit_to("page")

    def fit_width(self) -> None:
        self._fit_to("width")

    def zoom_for_page(self, page_size_pts: tuple[float, float]) -> float:
        """The logical zoom that WILL apply if this page is shown now (fit modes
        depend on the page size; free zoom is sticky)."""
        if self._fit_mode is None:
            return self._zoom
        return self._fit_zoom_for(page_size_pts, self._fit_mode)

    def flush_pending_render(self) -> None:
        """Emit any pending renderNeeded immediately (also used by tests)."""
        self._rezoom_timer.stop()
        self._emit_render_needed()

    def _set_zoom(self, zoom: float) -> None:
        self._fit_mode = None
        self._apply_scrollbar_policy()
        self._zoom = _clamp(zoom)
        self._apply_transform()
        # Manual zoom (Ctrl+wheel / buttons) always debounces — a rapid stream
        # of steps must coalesce into one crisp re-render.
        self._rezoom_timer.start(_REZOOM_DELAY_MS)

    def _fit_to(self, mode: str) -> None:
        if self._item is None or self._page_size_pts is None:
            return
        self._fit_mode = mode
        self._apply_scrollbar_policy()
        self._zoom = self._fit_zoom_for(self._page_size_pts, mode)
        self._apply_transform()
        self._schedule_rezoom()

    def _apply_scrollbar_policy(self) -> None:
        """In fit-PAGE the page fits both axes, so it never needs a scrollbar —
        force them OFF so a boundary window width (where rounding leaves the page
        a hair too big) can't make QGraphicsView.scale() flip a ScrollBarAsNeeded
        bar on/off and oscillate. Every other mode keeps AsNeeded so a zoomed-in
        page can be panned. A no-op when unchanged, so calling it per resize is
        cheap. (Complements the deferred-fit fix in resizeEvent.)"""
        if self._fit_mode == "page":
            off = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            self.setHorizontalScrollBarPolicy(off)
            self.setVerticalScrollBarPolicy(off)
        else:
            as_needed = Qt.ScrollBarPolicy.ScrollBarAsNeeded
            self.setHorizontalScrollBarPolicy(as_needed)
            self.setVerticalScrollBarPolicy(as_needed)

    def _schedule_rezoom(self) -> None:
        """Debounce a fit's re-render — but re-render on the NEXT event-loop
        turn when the current pixmap is the pre-layout placeholder, so it is
        never left blocky on screen (see _PLACEHOLDER_UPSCALE). NOTE: always
        pass an explicit interval — QTimer.start(msec) resets the interval, so
        a bare start() after a start(0) would inherit the 0."""
        placeholder = self._render_zoom > 0 and (
            self._zoom > self._render_zoom * _PLACEHOLDER_UPSCALE
        )
        self._rezoom_timer.start(0 if placeholder else _REZOOM_DELAY_MS)

    def _fit_zoom_for(self, page_size_pts: tuple[float, float], mode: str) -> float:
        vw = max(1, self.viewport().width() - 2)
        vh = max(1, self.viewport().height() - 2)
        pw, ph = page_size_pts
        if pw <= 0 or ph <= 0:
            return self._zoom
        zoom = vw / pw if mode == "width" else min(vw / pw, vh / ph)
        return _clamp(zoom)

    def _apply_transform(self) -> None:
        t = self._zoom / self._render_zoom
        self.resetTransform()
        self.scale(t, t)

    def _emit_render_needed(self) -> None:
        if self._item is not None:
            self.renderNeeded.emit(self._zoom)

    # --- events ---------------------------------------------------------
    def event(self, e: QEvent) -> bool:
        # The monitor's scale factor changed (window dragged to a different-DPI
        # screen, or Windows display scaling changed). The render zoom bakes in
        # the DPR, so request a crisp re-render at the new effective resolution
        # — without this, a free-zoom page would sit resampled/blurry at rest.
        if e.type() == QEvent.Type.DevicePixelRatioChange and self._item is not None:
            self._rezoom_timer.start(_REZOOM_DELAY_MS)
        return super().event(e)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_mode is not None and self._item is not None:
            # Defer the refit — NEVER scale() inside resizeEvent (see the
            # _resize_fit_timer note). A single dock lands the fit on the next
            # event-loop turn (imperceptible); a resize stream coalesces.
            self._resize_fit_timer.start(0)
        if self._armed_chip.isVisible():
            self._position_chip()

    def _apply_resize_fit(self) -> None:
        """Run the deferred resize refit (off the resizeEvent stack)."""
        if self._fit_mode is not None and self._item is not None:
            self._fit_to(self._fit_mode)

    @property
    def hover_kind(self) -> str | None:
        return self._hover_kind

    @property
    def insert_armed(self) -> bool:
        return self._insert_armed

    @property
    def region_armed(self) -> bool:
        return self._region_armed

    @property
    def region_click_count(self) -> int:
        """Consecutive-click count for the last highlight click (1/2 word, 3 line)."""
        return self._region_click_count

    @property
    def link_rect_armed(self) -> bool:
        return self._linkrect_armed

    @property
    def sign_rect_armed(self) -> bool:
        return self._signrect_armed

    @property
    def link_armed(self) -> bool:
        return self._link_armed

    @property
    def link_clicks(self) -> int:
        """Consecutive-click count for the last Hyperlink press (1/2 word, 3 sentence)."""
        return self._link_clicks

    @property
    def is_armed(self) -> bool:
        return (
            self._insert_armed
            or self._region_armed
            or self._linkrect_armed
            or self._signrect_armed
            or self._link_armed
        )

    def arm_link(self, chip_text: str = "") -> None:
        """Arm the merged Hyperlink tool. STAYS armed (so a double/triple click
        can accumulate) until the view disarms it or Esc cancels."""
        self.clear_hover()
        self._insert_armed = False  # modes are mutually exclusive
        self._region_armed = False
        self._region_press = None
        self._linkrect_armed = False
        self._signrect_armed = False
        self._signrect_press = None
        self._link_armed = True
        self._link_press = None
        self._link_clicks = 0
        self._link_click_scene = None
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self._show_chip(chip_text)
        self.armedChanged.emit()

    def disarm_link(self) -> None:
        was = self._link_armed
        self._link_armed = False
        self._link_press = None
        self._link_rect_mode = False
        self._link_accepted = False
        self._link_clicks = 0
        self._link_click_scene = None
        self.viewport().unsetCursor()
        if was:
            self._armed_chip.hide()
            self.armedChanged.emit()

    def begin_link_flow(self) -> None:
        """Accept the pending Hyperlink press as a TEXT-FLOW drag (no rubber
        band — the view paints the selection highlight)."""
        self._link_accepted = True
        self._link_rect_mode = False

    def begin_link_rect(self) -> None:
        """Accept the pending Hyperlink press as a RECTANGLE drag (over blank
        space or an image), drawn with the shared rubber band."""
        self._link_accepted = True
        self._link_rect_mode = True
        if self._link_press is None:
            return
        if self._move_band is None:
            self._move_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        anchor = self.mapFromScene(self._link_press)
        self._move_band.setGeometry(QRect(anchor, anchor))
        self._move_band.show()

    def _bump_link_click(self, scene_pos: QPointF) -> None:
        """Advance the Hyperlink click counter (same near-and-recent rule the
        highlighter uses): 1/2 = the word, 3 = the sentence."""
        interval = QApplication.doubleClickInterval()
        now = _now_ms()
        near = self._link_click_scene is not None and (
            abs(scene_pos.x() - self._link_click_scene.x()) < 6.0
            and abs(scene_pos.y() - self._link_click_scene.y()) < 6.0
        )
        self._link_clicks = (
            self._link_clicks + 1 if near and (now - self._link_click_ms) <= interval else 1
        )
        self._link_click_ms = now
        self._link_click_scene = scene_pos

    def arm_insert_point(self, chip_text: str = "") -> None:
        """One-shot mode: the next left-click on the page picks a point."""
        self.clear_hover()  # armed modes own the cursor
        self._region_armed = False  # modes are mutually exclusive
        self._region_press = None
        self._linkrect_armed = False
        self._signrect_armed = False
        self._signrect_press = None
        self._link_armed = False
        self._insert_armed = True
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self._show_chip(chip_text)
        self.armedChanged.emit()

    def arm_link_rect(self, chip_text: str = "") -> None:
        """One-shot mode: press-drag-release draws the new link's rectangle,
        reported via ``linkRectSelected`` (mirrors arm_insert_point, but the
        gesture is a drag). Disarms on release."""
        self.clear_hover()  # armed modes own the cursor
        self._insert_armed = False  # modes are mutually exclusive
        self._region_armed = False
        self._region_press = None
        self._link_armed = False
        self._signrect_armed = False
        self._signrect_press = None
        self._linkrect_press = None
        self._linkrect_armed = True
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self._show_chip(chip_text)
        self.armedChanged.emit()

    def disarm_link_rect(self) -> None:
        was = self._linkrect_armed
        drag_live = self._linkrect_press is not None
        self._linkrect_armed = False
        self._linkrect_press = None
        self.viewport().unsetCursor()
        # Esc mid-drag: the release branch (the usual band-hider) will skip —
        # its press field is already None — so hide the band here.
        if drag_live and self._move_band is not None:
            self._move_band.hide()
        if was:
            self._armed_chip.hide()
            self.armedChanged.emit()

    def arm_sign_rect(self, chip_text: str = "") -> None:
        """One-shot mode: press-drag-release draws the signature's rectangle,
        reported via ``signRectSelected`` (the sign flow's twin of
        arm_link_rect). Disarms on release."""
        self.clear_hover()  # armed modes own the cursor
        self._insert_armed = False  # modes are mutually exclusive
        self._region_armed = False
        self._region_press = None
        self._link_armed = False
        self._linkrect_armed = False
        self._linkrect_press = None
        self._signrect_press = None
        self._signrect_armed = True
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self._show_chip(chip_text)
        self.armedChanged.emit()

    def disarm_sign_rect(self) -> None:
        was = self._signrect_armed
        drag_live = self._signrect_press is not None
        self._signrect_armed = False
        self._signrect_press = None
        self.viewport().unsetCursor()
        # Esc mid-drag: the release branch will skip its band-hide (press is
        # already None), so hide it here.
        if drag_live and self._move_band is not None:
            self._move_band.hide()
        if was:
            self._armed_chip.hide()
            self.armedChanged.emit()

    def disarm_insert_point(self) -> None:
        was = self._insert_armed
        self._insert_armed = False
        self.viewport().unsetCursor()
        if was:
            self._armed_chip.hide()
            self.armedChanged.emit()

    def arm_region_select(self, chip_text: str = "", sticky: bool = False) -> None:
        """Arm window selection: press-drag-release marks a window on the page,
        a click/double-click marks the word, a triple-click the line. When
        ``sticky`` the tool STAYS armed after each mark (highlighter pen) until
        Esc / a click off the page / re-triggering the action."""
        self.clear_hover()  # armed modes own the cursor
        self._insert_armed = False  # modes are mutually exclusive
        self._linkrect_armed = False
        self._signrect_armed = False
        self._signrect_press = None
        self._link_armed = False
        self._region_armed = True
        self._region_sticky = sticky
        self._region_click_count = 0
        self._region_click_scene = None
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self._show_chip(chip_text)
        self.armedChanged.emit()

    def disarm_region_select(self) -> None:
        was = self._region_armed
        drag_live = self._region_press is not None
        self._region_armed = False
        self._region_sticky = False
        self._region_press = None
        self._region_click_count = 0
        self._region_click_scene = None
        self.viewport().unsetCursor()
        # Esc mid-drag: the release branch will skip its band-hide (press is
        # already None), so hide it here.
        if drag_live and self._move_band is not None:
            self._move_band.hide()
        if was:
            self._armed_chip.hide()
            self.armedChanged.emit()

    def _bump_region_click(self, scene_pos: QPointF) -> None:
        """Advance the highlight click counter: consecutive clicks near the same
        spot within the double-click interval accumulate (1/2 = word, 3 = line);
        a distant or late click restarts at 1."""
        interval = QApplication.doubleClickInterval()
        now = _now_ms()
        near = self._region_click_scene is not None and (
            abs(scene_pos.x() - self._region_click_scene.x()) < 6.0
            and abs(scene_pos.y() - self._region_click_scene.y()) < 6.0
        )
        if near and (now - self._region_click_ms) <= interval:
            self._region_click_count += 1
        else:
            self._region_click_count = 1
        self._region_click_ms = now
        self._region_click_scene = scene_pos

    # --- armed-mode chip (U4) ----------------------------------------------
    def _show_chip(self, text: str) -> None:
        if not text:
            self._armed_chip.hide()
            return
        self._armed_chip.setText(text)
        self._armed_chip.setStyleSheet(theme.armed_chip_qss())
        self._armed_chip.adjustSize()
        self._position_chip()
        self._armed_chip.show()
        self._armed_chip.raise_()

    def _position_chip(self) -> None:
        width = self.viewport().width()
        self._armed_chip.move(max(0, (width - self._armed_chip.width()) // 2), 8)

    def refresh_chip_theme(self) -> None:
        """Restyle a visible chip after a theme switch (view.refresh_theme)."""
        if self._armed_chip.isVisible():
            self._armed_chip.setStyleSheet(theme.armed_chip_qss())

    def begin_move_feedback(self, scene_rect: tuple[float, float, float, float]) -> None:
        """Accept the pending Ctrl+drag; the rubber band tracks this rect."""
        x0, y0, x1, y1 = scene_rect
        self._move_base_rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        self._resize_anchor = None

    def begin_resize_feedback(self, anchor_scene: tuple[float, float]) -> None:
        """Accept the pending Ctrl+drag as a RESIZE: the rubber band spans from
        the fixed anchor corner (scene px) to the cursor."""
        self._move_base_rect = QRectF()  # non-None so the press is accepted
        self._resize_anchor = QPointF(anchor_scene[0], anchor_scene[1])

    def begin_text_selection(self, sx: float, sy: float) -> None:
        """Accept the pending plain press as a read-only window/marquee text
        selection (X4). The view calls this synchronously from its
        ``selectDragStarted`` handler; the canvas draws a rubber-band rectangle
        while dragging and reports live positions via ``textSelectMoved`` and
        the release via ``textSelectFinished`` (the view selects the text
        inside the rectangle).
        """
        self._text_select_press = QPointF(sx, sy)
        if self._move_band is None:
            self._move_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        anchor = self.mapFromScene(self._text_select_press)
        self._move_band.setGeometry(QRect(anchor, anchor))
        self._move_band.show()

    def begin_box_marquee(self, sx: float, sy: float) -> None:
        """Accept the pending plain press as an edit-mode box marquee (task 1).

        The view calls this synchronously from its ``selectDragStarted``
        handler when the press lands on EMPTY page area in edit mode. The
        canvas draws a rubber-band rectangle while dragging and reports the
        release corners via ``boxMarqueeFinished``; the view decides which
        boxes fall in it (window vs crossing) and how the modifier folds them
        into the multi-selection."""
        self._box_marquee_press = QPointF(sx, sy)
        if self._move_band is None:
            self._move_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        anchor = self.mapFromScene(self._box_marquee_press)
        self._move_band.setGeometry(QRect(anchor, anchor))
        self._move_band.show()

    def _axis_snapped(self, current: QPointF, press: QPointF | None = None) -> QPointF:
        """With Shift held, snap a move to its dominant axis (horizontal OR
        vertical), so a nudge keeps an existing column/row alignment.

        Scene space == on-screen axes (the pixmap is rotation-applied), so a
        pure-x or pure-y scene delta is a pure-x/pure-y SCREEN move; the view's
        ``_drag_offset`` maps it back to a rotation-correct page offset. The
        larger component wins ties toward horizontal. Modifiers are read LIVE
        (U-series rule — never tracked via key events), so holding Shift PART
        WAY through a drag snaps from that point and releasing it unsnaps."""
        press = press if press is not None else self._move_press
        shift = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        if press is None or not shift:
            return current
        dx = current.x() - press.x()
        dy = current.y() - press.y()
        if abs(dx) >= abs(dy):
            return QPointF(current.x(), press.y())  # horizontal only
        return QPointF(press.x(), current.y())  # vertical only

    # --- hover affordances (U2a) ------------------------------------------
    def set_hover(
        self,
        kind: str,
        scene_rect: tuple[float, float, float, float],
        corner_scene: tuple[float, float] | None = None,
        corner_zone_scene: float = 0.0,
    ) -> None:
        """Show hover feedback (called synchronously from ``hoverMoved``)."""
        x0, y0, x1, y1 = scene_rect
        rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        corner = QPointF(*corner_scene) if corner_scene is not None else None
        state = (kind, rect, corner, corner_zone_scene)
        if state == (self._hover_kind, self._hover_rect, self._hover_corner, self._hover_zone):
            # Unchanged element — but the cursor may still need to flip: the
            # Ctrl variants read the LIVE modifier state each move (U2b).
            self.viewport().setCursor(self._hover_cursor())
            return
        kind_changed = kind != self._hover_kind
        self._hover_kind, self._hover_rect, self._hover_corner, self._hover_zone = state
        self.viewport().setCursor(self._hover_cursor())
        self.viewport().update()
        if kind_changed:
            self.hoverKindChanged.emit(kind)

    def set_text_hover(self, on: bool) -> None:
        """Read-only text selection (X4): show the I-beam over selectable words.

        Independent of the edit-mode hover outline (which read-only never
        shows); reset by ``clear_hover`` / ``leaveEvent`` so the cursor never
        sticks off a word or off the page.
        """
        on = bool(on)
        if on == self._text_hover:
            return
        self._text_hover = on
        if on:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.viewport().unsetCursor()

    def clear_hover(self) -> None:
        if self._text_hover:
            self._text_hover = False
            self.viewport().unsetCursor()
        if self._link_hover:
            self._link_hover = False
            self.viewport().unsetCursor()
        if self._hover_kind is None:
            return
        self._hover_kind = None
        self._hover_rect = None
        self._hover_corner = None
        self._hover_zone = 0.0
        self.viewport().unsetCursor()
        self.viewport().update()
        self.hoverKindChanged.emit("")

    def _hover_cursor(self) -> Qt.CursorShape:
        # Ctrl variants read the LIVE modifier state — never tracked key
        # events, which stick when focus leaves with Ctrl held (a stationary
        # Ctrl press updates on the next mouse move; accepted).
        ctrl = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
        if self._hover_kind in ("image_corner", "link_corner"):
            rect, corner = self._hover_rect, self._hover_corner
            if rect is not None and corner is not None:
                # SCENE-side comparison keeps the diagonal right on rotated
                # pages (a page-space top-left corner can render anywhere).
                left = corner.x() < rect.center().x()
                top = corner.y() < rect.center().y()
                if left == top:
                    return Qt.CursorShape.SizeFDiagCursor
                return Qt.CursorShape.SizeBDiagCursor
        if ctrl:  # Ctrl+drag moves text paragraphs and image bodies
            return Qt.CursorShape.SizeAllCursor
        if self._hover_kind == "text":
            return Qt.CursorShape.IBeamCursor
        return Qt.CursorShape.OpenHandCursor

    def _update_hover(self, viewport_point) -> None:
        # The Hyperlink tool deliberately KEEPS hover alive (unlike the other
        # armed modes, which own the cursor): the view needs it to show an
        # I-beam over text and a crosshair over blank space, so you can see
        # what a drag would do. A live drag still suppresses it.
        if (
            self._item is None
            or self._insert_armed
            or self._region_armed
            or self._linkrect_armed
            or self._signrect_armed
            or self._move_press is not None
            or self._link_press is not None
        ):
            return
        scene_pos = self.mapToScene(viewport_point)
        if not self._item.boundingRect().contains(scene_pos):
            self.clear_hover()
            return
        self.hoverMoved.emit(scene_pos.x(), scene_pos.y())

    def _refresh_hover(self) -> None:
        """Re-resolve hover after the pixmap changed under a resting cursor
        (page switch or crisper re-render: the scene scale changed, so the
        stored scene rect no longer matches the geometry it outlined)."""
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        if self.viewport().rect().contains(pos):
            self._update_hover(pos)
        else:
            self.clear_hover()

    # --- selection chrome (U6) --------------------------------------------
    def set_selection(self, kind: str, scene_rect: tuple[float, float, float, float]) -> None:
        """Show selection chrome (the view owns the actual selection)."""
        x0, y0, x1, y1 = scene_rect
        rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        if (kind, rect) == (self._selection_kind, self._selection_rect):
            return
        self._selection_kind = kind
        self._selection_rect = rect
        self.viewport().update()

    def set_multi_selection(self, scene_rects: list[tuple[float, float, float, float]]) -> None:
        """Show multi-selection chrome — one border per selected box (E10.7)."""
        rects = [QRectF(x0, y0, x1 - x0, y1 - y0) for (x0, y0, x1, y1) in scene_rects]
        if rects == self._multi_selection_rects:
            return
        self._multi_selection_rects = rects
        self.viewport().update()

    def clear_selection(self) -> None:
        if self._selection_kind is None and not self._multi_selection_rects:
            return
        self._selection_kind = None
        self._selection_rect = None
        self._multi_selection_rects = []
        self.viewport().update()

    # --- reveal-all outlines (U5) -------------------------------------------
    def set_reveal_rects(self, scene_rects: list[tuple[float, float, float, float]]) -> None:
        """Outline every editable area (empty list clears)."""
        rects = [QRectF(x0, y0, x1 - x0, y1 - y0) for (x0, y0, x1, y1) in scene_rects]
        if rects == self._reveal_rects:
            return
        self._reveal_rects = rects
        self.viewport().update()

    # --- hyperlink hotspots -------------------------------------------------
    def set_link_rects(self, scene_rects: list[tuple[float, float, float, float]]) -> None:
        """Outline every hyperlink hotspot in the link colour (empty clears)."""
        rects = [QRectF(x0, y0, x1 - x0, y1 - y0) for (x0, y0, x1, y1) in scene_rects]
        if rects == self._link_rects:
            return
        self._link_rects = rects
        self.viewport().update()

    def set_link_tool_cursor(self, over_text: bool) -> None:
        """While the Hyperlink tool is armed: I-beam over text (a drag selects a
        run), crosshair elsewhere (a drag draws a rectangle) — so the gesture
        the press will start is visible before you commit to it."""
        if not self._link_armed:
            return
        self.viewport().setCursor(
            Qt.CursorShape.IBeamCursor if over_text else Qt.CursorShape.CrossCursor
        )

    def set_link_hover(self, on: bool) -> None:
        """Read-only follow affordance: a pointing-hand cursor over a link.

        Independent of the edit-mode hover outline, like ``set_text_hover``;
        reset by ``clear_hover`` / ``leaveEvent`` so the cursor never sticks."""
        on = bool(on)
        if on == self._link_hover:
            return
        self._link_hover = on
        if on:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().unsetCursor()

    # --- search highlights (SR2) ------------------------------------------
    def set_search_hits(
        self,
        scene_rects: list[tuple[float, float, float, float]],
        current_rects: list[tuple[float, float, float, float]],
    ) -> None:
        """Highlight this page's search hits; the CURRENT hit draws stronger."""
        rects = [QRectF(x0, y0, x1 - x0, y1 - y0) for (x0, y0, x1, y1) in scene_rects]
        current = [QRectF(x0, y0, x1 - x0, y1 - y0) for (x0, y0, x1, y1) in current_rects]
        if rects == self._search_rects and current == self._search_current:
            return
        self._search_rects = rects
        self._search_current = current
        self.viewport().update()

    def clear_search_hits(self) -> None:
        if not self._search_rects and not self._search_current:
            return
        self._search_rects = []
        self._search_current = []
        self.viewport().update()

    # --- read-only text-selection highlight (X4) --------------------------
    def set_text_selection_rects(
        self, scene_rects: list[tuple[float, float, float, float]]
    ) -> None:
        """Show the read-only text selection's per-line rects (empty clears)."""
        rects = [QRectF(x0, y0, x1 - x0, y1 - y0) for (x0, y0, x1, y1) in scene_rects]
        if rects == self._text_selection_rects:
            return
        self._text_selection_rects = rects
        self.viewport().update()

    def clear_text_selection(self) -> None:
        if not self._text_selection_rects:
            return
        self._text_selection_rects = []
        self.viewport().update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            # Reaches here only for clicks NOT on the editor overlay (a child
            # of the viewport) — so a plain click on the page means "done".
            self.backgroundPressed.emit()
        if (
            self._region_armed
            and event.button() == Qt.MouseButton.LeftButton
            and self._item is not None
        ):
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._item.boundingRect().contains(scene_pos):
                self._bump_region_click(scene_pos)  # 1st / 3rd click of a sequence
                self._region_press = scene_pos
                if self._move_band is None:
                    self._move_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                anchor = self.mapFromScene(scene_pos)
                self._move_band.setGeometry(QRect(anchor, anchor))
                self._move_band.show()
                event.accept()
                return
            self.disarm_region_select()  # clicked off the page — cancel
        if (
            self._link_armed
            and event.button() == Qt.MouseButton.LeftButton
            and self._item is not None
        ):
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._item.boundingRect().contains(scene_pos):
                self._bump_link_click(scene_pos)
                self._link_press = scene_pos
                self._link_accepted = False
                # Synchronous: the view hit-tests and accepts as a text-flow or
                # a rectangle drag before this returns.
                self.linkDragStarted.emit(scene_pos.x(), scene_pos.y())
                if self._link_accepted:
                    event.accept()
                    return
                self._link_press = None
            else:
                self.disarm_link()  # clicked off the page — cancel
        if (
            self._linkrect_armed
            and event.button() == Qt.MouseButton.LeftButton
            and self._item is not None
        ):
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._item.boundingRect().contains(scene_pos):
                self._linkrect_press = scene_pos
                if self._move_band is None:
                    self._move_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                anchor = self.mapFromScene(scene_pos)
                self._move_band.setGeometry(QRect(anchor, anchor))
                self._move_band.show()
                event.accept()
                return
            self.disarm_link_rect()  # clicked off the page — cancel
        if (
            self._signrect_armed
            and event.button() == Qt.MouseButton.LeftButton
            and self._item is not None
        ):
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._item.boundingRect().contains(scene_pos):
                self._signrect_press = scene_pos
                if self._move_band is None:
                    self._move_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                anchor = self.mapFromScene(scene_pos)
                self._move_band.setGeometry(QRect(anchor, anchor))
                self._move_band.show()
                event.accept()
                return
            self.disarm_sign_rect()  # clicked off the page — cancel
        if (
            self._insert_armed
            and event.button() == Qt.MouseButton.LeftButton
            and self._item is not None
        ):
            scene_pos = self.mapToScene(event.position().toPoint())
            self.disarm_insert_point()  # one-shot either way
            if self._item.boundingRect().contains(scene_pos):
                # A double-click's second press would otherwise ALSO fire the
                # double-click edit path right after the armed action (review
                # finding: insert-image + dblclick immediately re-prompted to
                # replace the image just placed).
                self._suppress_dblclick = True
                self.insertPointSelected.emit(scene_pos.x(), scene_pos.y())
                event.accept()
                return
        self._suppress_dblclick = False  # any normal press clears the guard
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and self._item is not None
        ):
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._item.boundingRect().contains(scene_pos):
                self._move_press = scene_pos
                self._move_base_rect = None
                # Synchronous: the view hit-tests and may accept via
                # begin_move_feedback() before this returns.
                self.moveDragStarted.emit(scene_pos.x(), scene_pos.y())
                if self._move_base_rect is not None:
                    event.accept()
                    return
                self._move_press = None
        if event.button() == Qt.MouseButton.LeftButton and self._item is not None:
            # Plain press (U6): the view selects what's under it, and for a
            # press on the ALREADY-selected element accepts a move/resize
            # drag through the same protocol as the Ctrl branch above.
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._item.boundingRect().contains(scene_pos):
                self._move_press = scene_pos
                self._move_base_rect = None
                self._text_select_press = None
                self._box_marquee_press = None
                self.selectDragStarted.emit(scene_pos.x(), scene_pos.y())
                if self._move_base_rect is not None:  # edit: move/resize accepted
                    event.accept()
                    return
                if self._text_select_press is not None:  # read-only: text selection
                    self._move_press = None
                    event.accept()
                    return
                if self._box_marquee_press is not None:  # edit: box marquee (task 1)
                    self._move_press = None
                    event.accept()
                    return
                self._move_press = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._region_press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = self.mapToScene(event.position().toPoint())
            top_left = self.mapFromScene(self._region_press)
            bottom_right = self.mapFromScene(current)
            if self._move_band is not None:
                self._move_band.setGeometry(QRect(top_left, bottom_right).normalized())
            event.accept()
            return
        if self._linkrect_press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = self.mapToScene(event.position().toPoint())
            top_left = self.mapFromScene(self._linkrect_press)
            bottom_right = self.mapFromScene(current)
            if self._move_band is not None:
                self._move_band.setGeometry(QRect(top_left, bottom_right).normalized())
            event.accept()
            return
        if self._signrect_press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = self.mapToScene(event.position().toPoint())
            top_left = self.mapFromScene(self._signrect_press)
            bottom_right = self.mapFromScene(current)
            if self._move_band is not None:
                self._move_band.setGeometry(QRect(top_left, bottom_right).normalized())
            event.accept()
            return
        if self._link_press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = self.mapToScene(event.position().toPoint())
            if self._link_rect_mode:  # blank area / image -> rubber-band rect
                if self._move_band is not None:
                    top_left = self.mapFromScene(self._link_press)
                    self._move_band.setGeometry(
                        QRect(top_left, self.mapFromScene(current)).normalized()
                    )
            else:  # over text -> the view extends + paints the flow selection
                self.linkDragMoved.emit(current.x(), current.y())
            event.accept()
            return
        if (
            self._move_press is not None
            and self._move_base_rect is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            current = self.mapToScene(event.position().toPoint())
            if self._resize_anchor is not None:  # resize: anchor corner -> cursor
                top_left = self.mapFromScene(self._resize_anchor)
                bottom_right = event.position().toPoint()
            else:  # move: translate the whole rect (Shift snaps to one axis)
                current = self._axis_snapped(current)
                shifted = self._move_base_rect.translated(current - self._move_press)
                top_left = self.mapFromScene(shifted.topLeft())
                bottom_right = self.mapFromScene(shifted.bottomRight())
            if self._move_band is None:
                self._move_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
            self._move_band.setGeometry(QRect(top_left, bottom_right).normalized())
            self._move_band.show()
            event.accept()
            return
        if self._text_select_press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            # Read-only window selection drag (X4): grow the rubber band and
            # report the live cursor point; the view selects the text inside.
            current = self.mapToScene(event.position().toPoint())
            if self._move_band is not None:
                top_left = self.mapFromScene(self._text_select_press)
                bottom_right = self.mapFromScene(current)
                self._move_band.setGeometry(QRect(top_left, bottom_right).normalized())
            self.textSelectMoved.emit(current.x(), current.y())
            event.accept()
            return
        if self._box_marquee_press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            # Edit-mode box marquee (task 1): grow the band. Selection is
            # resolved on release (window vs crossing by drag direction).
            current = self.mapToScene(event.position().toPoint())
            if self._move_band is not None:
                top_left = self.mapFromScene(self._box_marquee_press)
                bottom_right = self.mapFromScene(current)
                self._move_band.setGeometry(QRect(top_left, bottom_right).normalized())
            event.accept()
            return
        if not event.buttons():
            self._update_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.clear_hover()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl+C copies the read-only text selection (X4). An open in-place
            # editor holds focus in edit mode, so its own copy is unaffected.
            self.copyRequested.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deleteSelectionRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._region_press is not None and event.button() == Qt.MouseButton.LeftButton:
            press = self._region_press
            self._region_press = None
            if not self._region_sticky:
                self.disarm_region_select()  # one-shot: done after this mark
            if self._move_band is not None:
                self._move_band.hide()
            current = self.mapToScene(event.position().toPoint())
            self._suppress_dblclick = True  # eat a dblclick right after release
            self.regionSelected.emit(press.x(), press.y(), current.x(), current.y())
            event.accept()
            return
        if self._link_press is not None and event.button() == Qt.MouseButton.LeftButton:
            press = self._link_press
            self._link_press = None
            self._link_accepted = False
            if self._move_band is not None:
                self._move_band.hide()
            current = self.mapToScene(event.position().toPoint())
            # STAYS armed: the view decides when the gesture is complete (a drag
            # is final; a click waits out the double-click interval so 2 and 3
            # clicks can still arrive) and disarms then.
            self.linkDragFinished.emit(
                press.x(), press.y(), current.x(), current.y(), self._link_clicks
            )
            event.accept()
            return
        if self._linkrect_press is not None and event.button() == Qt.MouseButton.LeftButton:
            press = self._linkrect_press
            self._linkrect_press = None
            self.disarm_link_rect()  # one-shot: done after this rectangle
            if self._move_band is not None:
                self._move_band.hide()
            current = self.mapToScene(event.position().toPoint())
            self._suppress_dblclick = True  # eat a dblclick right after release
            self.linkRectSelected.emit(press.x(), press.y(), current.x(), current.y())
            event.accept()
            return
        if self._signrect_press is not None and event.button() == Qt.MouseButton.LeftButton:
            press = self._signrect_press
            self._signrect_press = None
            self.disarm_sign_rect()  # one-shot: done after this rectangle
            if self._move_band is not None:
                self._move_band.hide()
            current = self.mapToScene(event.position().toPoint())
            self._suppress_dblclick = True  # eat a dblclick right after release
            self.signRectSelected.emit(press.x(), press.y(), current.x(), current.y())
            event.accept()
            return
        if (
            self._move_press is not None
            and self._move_base_rect is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            press = self._move_press
            was_resize = self._resize_anchor is not None
            self._move_press = None
            self._move_base_rect = None
            self._resize_anchor = None
            if self._move_band is not None:
                self._move_band.hide()
            current = self.mapToScene(event.position().toPoint())
            if not was_resize:  # Shift snaps the committed offset to one axis too
                current = self._axis_snapped(current, press)
            self.moveDragFinished.emit(press.x(), press.y(), current.x(), current.y())
            event.accept()
            return
        if self._text_select_press is not None and event.button() == Qt.MouseButton.LeftButton:
            self._text_select_press = None
            if self._move_band is not None:
                self._move_band.hide()
            current = self.mapToScene(event.position().toPoint())
            self.textSelectFinished.emit(current.x(), current.y())
            event.accept()
            return
        if self._box_marquee_press is not None and event.button() == Qt.MouseButton.LeftButton:
            press = self._box_marquee_press
            self._box_marquee_press = None
            if self._move_band is not None:
                self._move_band.hide()
            current = self.mapToScene(event.position().toPoint())
            self.boxMarqueeFinished.emit(press.x(), press.y(), current.x(), current.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self._item is not None:
            scene_pos = self.mapToScene(event.pos())
            if self._item.boundingRect().contains(scene_pos):
                self.contextMenuRequested.emit(scene_pos.x(), scene_pos.y())
                event.accept()
                return
        super().contextMenuEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # Double-click on the page begins an in-place edit (no mode toggle —
        # single clicks and drags keep their scroll/select behaviour).
        # Ctrl+double-click edits the whole paragraph instead of one span.
        self._text_select_press = None  # a dblclick's release must not clear it (X4)
        self._box_marquee_press = None  # ditto for a box marquee (task 1)
        if self._move_band is not None:
            self._move_band.hide()  # drop any marquee band from the first press
        if self._region_armed and event.button() == Qt.MouseButton.LeftButton:
            # The 2nd click of a highlight double/triple sequence: count it (so a
            # following press reaches count 3 = whole line) and consume it — the
            # word was already marked on the first click's release.
            self._region_click_count += 1
            self._region_click_ms = _now_ms()
            self._region_click_scene = self.mapToScene(event.position().toPoint())
            self._suppress_dblclick = False
            event.accept()
            return
        if self._link_armed and event.button() == Qt.MouseButton.LeftButton:
            # 2nd click of a Hyperlink double/triple sequence — Qt swallows the
            # press, so count it here or a triple-click could never reach 3.
            self._link_clicks += 1
            self._link_click_ms = _now_ms()
            self._link_click_scene = self.mapToScene(event.position().toPoint())
            self._suppress_dblclick = False
            event.accept()
            return
        if self._suppress_dblclick:
            self._suppress_dblclick = False
            event.accept()
            return
        if self._item is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._item.boundingRect().contains(scene_pos):
                block = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self.pointActivated.emit(scene_pos.x(), scene_pos.y(), block)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        if self._search_rects or self._search_current:
            # Search highlights paint FIRST — content-like fill sits beneath
            # the interaction chrome (hover/selection outlines stay readable).
            accent = QColor(theme.accent())
            fill = QColor(accent)
            fill.setAlpha(50)
            pen = QPen(accent)
            pen.setCosmetic(True)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(fill)
            for hit in self._search_rects:
                painter.drawRect(hit)
            strong = QColor(accent)
            strong.setAlpha(110)
            current_pen = QPen(accent)
            current_pen.setCosmetic(True)
            current_pen.setWidthF(2.0)
            painter.setPen(current_pen)
            painter.setBrush(strong)
            for hit in self._search_current:
                painter.drawRect(hit)
        if self._text_selection_rects:  # read-only window/marquee selection (X4)
            accent = QColor(theme.accent())
            fill = QColor(accent)
            fill.setAlpha(70)
            pen = QPen(accent)
            pen.setCosmetic(True)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(fill)
            for rect in self._text_selection_rects:
                painter.drawRect(rect)
        if self._reveal_rects:  # faint dashed underlay — hover/selection on top
            accent = QColor(theme.accent())
            accent.setAlpha(120)
            pen = QPen(accent)
            pen.setCosmetic(True)
            pen.setWidthF(1.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for reveal in self._reveal_rects:
                painter.drawRect(reveal)
        if self._link_rects:  # hyperlink hotspots — a DISTINCT (teal) colour
            line = QColor(_LINK_COLOR)
            pen = QPen(line)
            pen.setCosmetic(True)
            pen.setWidthF(1.5)
            painter.setPen(pen)
            fill = QColor(line)
            fill.setAlpha(28)
            painter.setBrush(fill)
            for link in self._link_rects:
                painter.drawRect(link)
        if self._hover_rect is not None:
            accent = QColor(theme.accent())
            pen = QPen(accent)
            pen.setCosmetic(True)  # constant on-screen width at any zoom
            pen.setWidthF(1.5)
            painter.setPen(pen)
            fill = QColor(accent)
            fill.setAlpha(22)
            painter.setBrush(fill)
            painter.drawRect(self._hover_rect)
            if self._hover_kind in ("image", "image_corner") and self._hover_zone > 0:
                self._draw_corner_ticks(painter, self._hover_rect, self._hover_zone)
        self._draw_selection(painter)  # selection chrome paints on top

    def _draw_selection(self, painter: QPainter) -> None:
        """Selection chrome (U6): solid border; corner handles on images.
        Multi-selection (E10.7): one solid border per member."""
        if self._multi_selection_rects:
            pen = QPen(QColor(theme.accent()))
            pen.setCosmetic(True)
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for member in self._multi_selection_rects:
                painter.drawRect(member)
        rect = self._selection_rect
        if rect is None:
            return
        accent = QColor(theme.accent())
        pen = QPen(accent)
        pen.setCosmetic(True)
        pen.setWidthF(2.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        if self._selection_kind not in ("image", "link"):
            return
        # Filled square handles, constant ON-SCREEN size (scene px shrink as
        # the view transform scales up).
        t = self._zoom / self._render_zoom if self._render_zoom else 1.0
        half = (8.0 / t) / 2 if t > 0 else 4.0
        painter.setBrush(accent)
        for corner in (rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
            painter.drawRect(QRectF(corner.x() - half, corner.y() - half, 2 * half, 2 * half))

    def _draw_corner_ticks(self, painter: QPainter, rect: QRectF, zone: float) -> None:
        """L-marks along the box edges: the corner-resize grab zones, made
        visible (the same min(18pt, w/3, h/3) rule the Ctrl+drag uses)."""
        pen = QPen(QColor(theme.accent()))
        pen.setCosmetic(True)
        pen.setWidthF(3.0)
        painter.setPen(pen)
        x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
        z = min(zone, rect.width() / 2, rect.height() / 2)
        for cx, cy, dx, dy in (
            (x0, y0, 1.0, 1.0),
            (x1, y0, -1.0, 1.0),
            (x0, y1, 1.0, -1.0),
            (x1, y1, -1.0, -1.0),
        ):
            painter.drawLine(QPointF(cx, cy), QPointF(cx + dx * z, cy))
            painter.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * z))

    def scroll_to_vertical_edge(self, top: bool) -> None:
        """Snap the vertical scroll to the top or bottom, at the current zoom.

        The page hand-off landing: scrolling onto the next page lands at its
        TOP, onto the previous page at its BOTTOM (no animation, no carry-over
        from the crossing scroll)."""
        sb = self.verticalScrollBar()
        sb.setValue(sb.minimum() if top else sb.maximum())

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
            return
        if event.modifiers() == Qt.KeyboardModifier.NoModifier and self._wheel_page_scroll(event):
            event.accept()
            return
        super().wheelEvent(event)

    def _wheel_page_scroll(self, event: QWheelEvent) -> bool:
        """Plain-scroll page navigation; True means the event is consumed.

        Mid-page vertical scrolling returns False untouched (the base class
        scrolls as usual). At the top/bottom scroll edge — which is both edges
        at once when the whole page fits — a scroll pressing outward requests
        a page flip. A discrete mouse notch (angle-only, at least one notch)
        flips per event; small-delta streams (trackpad pixels, sub-notch
        high-res wheels) accumulate to a deliberate-crossing threshold, and
        after a flip the rest of the stream — including the momentum tail —
        is swallowed until a quiet gap, so one continuous swipe moves ONE
        page and holds at the landing edge."""
        if self._item is None:
            return False
        pixels = event.pixelDelta().y()
        dy = pixels if pixels else event.angleDelta().y()
        if dy == 0:
            return False  # horizontal scroll — not ours
        now = _now_ms()
        gap = now - self._scroll_last_ms
        self._scroll_last_ms = now
        notch = pixels == 0 and abs(dy) >= _EDGE_THRESHOLD_ANGLE
        if self._scroll_hold and not notch:
            if gap < _EDGE_REARM_MS:
                return True  # same stream (momentum): hold at the landing edge
            self._scroll_hold = False  # quiet gap — a new gesture, re-armed
        sb = self.verticalScrollBar()
        if sb.maximum() - sb.minimum() <= _EDGE_FIT_SLACK_PX:
            at_edge = True  # page effectively fully visible: always a flip
        elif dy < 0:
            at_edge = sb.value() >= sb.maximum()
        else:
            at_edge = sb.value() <= sb.minimum()
        if not at_edge:
            self._scroll_accum = 0.0
            return False  # normal within-page scrolling
        direction = 1 if dy < 0 else -1
        if notch:  # a notch is deliberate by itself: one notch = one page
            self._scroll_accum = 0.0
            self.pageScrollRequested.emit(direction)
            return True
        kind = "px" if pixels else "angle"
        if (
            kind != self._scroll_accum_kind
            or gap >= _EDGE_REARM_MS
            or (self._scroll_accum > 0) != (dy > 0)
        ):
            self._scroll_accum = 0.0  # stream break / direction change
        self._scroll_accum_kind = kind
        self._scroll_accum += dy
        threshold = _EDGE_THRESHOLD_PX if kind == "px" else _EDGE_THRESHOLD_ANGLE
        if abs(self._scroll_accum) >= threshold:
            self._scroll_accum = 0.0
            self._scroll_hold = True  # one flip per stream: swallow the rest
            self.pageScrollRequested.emit(direction)
        return True  # an outward scroll at the edge never leaks to super()


def _clamp(zoom: float) -> float:
    return min(max(zoom, _MIN_ZOOM), _MAX_ZOOM)


def _now_ms() -> float:
    """Monotonic milliseconds (module-level so tests can fake the clock)."""
    return time.monotonic() * 1000.0
