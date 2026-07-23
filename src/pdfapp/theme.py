"""Central theming — the ONE styling authority for the app (qt-material).

``apply_theme`` is the only place in the codebase that may call
``qt_material.apply_stylesheet``. qt-material REPLACES the application
stylesheet on every apply, so this module always re-appends its own
addendum (``_qss_addendum(mode)`` — small hand-written fixes layered on top
of the theme; keep every hand-written chrome rule here, nowhere else).

The PDF page itself is a rendered pixmap: no stylesheet can touch it, so the
page stays true white in every mode. What this module does control:

- the app-wide qt-material theme (dark default, light supported);
- ``accent()`` — the one accent colour other modules may consume (the
  in-place editor overlays keep their deliberate light chrome; their border /
  grip colour folds into this accent at restyle S2);
- ``canvas_brush()`` — the backdrop behind the rendered page.

The theme choice is not persisted (no settings mechanism exists); every
launch starts dark.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QApplication
from qt_material import apply_stylesheet

DARK = "dark"
LIGHT = "light"

# Per mode: (qt-material theme file, invert_secondary). Blue family — stays in
# the family of the app's long-standing #4a90d9 accent. qt-material's light
# themes require invert_secondary=True (their docs) or text lands on white.
_THEMES: dict[str, tuple[str, bool]] = {
    DARK: ("dark_blue.xml", False),
    LIGHT: ("light_blue.xml", True),
}

# Used when accent() is asked before any apply_theme() call (tests, tools).
_FALLBACK_ACCENT = "#4a90d9"

# Backdrop behind the rendered page. Light mode keeps Qt.darkGray (#a0a0a4),
# the app's historical backdrop; dark mode uses a deeper neutral so the white
# page pops without the shell/backdrop contrast jumping.
_CANVAS = {DARK: "#2d2d30", LIGHT: "#a0a0a4"}


def _mix(over: str, under: str, weight: float) -> str:
    """Blend ``over`` onto ``under`` (weight = share of ``over``) to a SOLID
    hex. QSS rgba() would composite the border over the background and
    double-tint the edges — the exact side-bars this replaces."""
    a, b = QColor(over), QColor(under)
    return QColor(
        round(a.red() * weight + b.red() * (1 - weight)),
        round(a.green() * weight + b.green() * (1 - weight)),
        round(a.blue() * weight + b.blue() * (1 - weight)),
    ).name()


def _qss_addendum(mode: str) -> str:
    """Hand-written QSS layered ON TOP of qt-material — every chrome rule
    lives here, nowhere else. Mode-aware: the tab-strip band needs per-mode
    colours. The marker comment doubles as a test hook proving the addendum
    survived an apply.
    """
    dark = mode == DARK
    tab_strip = "#26292c" if dark else "#dfe3e6"  # a band DISTINCT from the toolbar
    tab_sep = "#1e2124" if dark else "#b8bec3"  # caps the ACTIVE tab off the toolbar
    tab_fg = "#e8eaec" if dark else "#37474f"
    tab_fg_dim = "#8a949c" if dark else "#78909c"
    tab_selected_bg = "#31363b" if dark else "#ffffff"
    # Material "selected icon button": a rounded tonal container, not
    # qt-material's fat accent side-bar. Solid blend of accent over shell.
    surface = "#31363b" if dark else "#ffffff"
    toggle_tint = _mix(accent(), surface, 0.28)
    # State layers for the same rounded container, one ladder weakest to
    # strongest: hover < checked < pressed < checked+hover < checked+pressed.
    # Every state a button can be in needs its own rung, because a CHECKED
    # button is also hovered when the pointer is on it — and the plain hover
    # rule outranks :checked, so without the last two rungs a selected button
    # dims to "not selected" the moment you point at it (caught by an offscreen
    # state render).
    hover_tint = _mix(accent(), surface, 0.12)
    press_tint = _mix(accent(), surface, 0.32)
    checked_hover_tint = _mix(accent(), surface, 0.38)
    checked_press_tint = _mix(accent(), surface, 0.46)
    return f"""
/* pdf-editor-addendum */

/* Tab strip: a distinct band under the toolbar; the ACTIVE tab is filled
   and accent-underlined, inactive tabs sit dimmer on the band. QTabWidget
   carries the band colour so the strip runs FULL width past the last tab,
   which also makes the toolbar's bottom separator read as ONE continuous
   line (qt-material's light-grey original vanished behind the tabs). The
   separator is load-bearing: the active tab's fill matches the toolbar
   tone, so without it the tab bleeds into the toolbar above. */
QToolBar:horizontal {{ border-bottom: 1px solid {tab_sep}; }}
QTabWidget {{ background: {tab_strip}; }}
QTabBar {{ background: {tab_strip}; }}
QTabBar::tab {{
    background: {tab_strip};
    color: {tab_fg_dim};
    padding: 7px 14px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover:!selected {{ color: {tab_fg}; }}
QTabBar::tab:selected {{
    background: {tab_selected_bg};
    color: {tab_fg};
    border-bottom: 2px solid {accent()};
}}

/* Toggle buttons (Material "selected icon button"): a rounded tonal fill
   with an accent glyph (icons.py color_on) replaces qt-material's 12px
   accent side-bar. Geometry note: the base button is margin 3 + border 12
   + padding 3 per side; rounding a box whose side borders are 12px while
   top/bottom are 0 leaves square notches at the corners, so the checked
   state moves the 12px into PADDING (background paints padding uniformly)
   — same footprint, clean rounded container. */
QToolButton:checked {{
    background: {toggle_tint};
    border: none;
    padding: 3px 15px;
    border-radius: 6px;
}}

/* Dropdown tool buttons (InstantPopup — the highlighter swatch and the
   alignment button, the ONLY popupMode="2" buttons on any toolbar). Same
   width as a plain icon button, with the base style's small corner
   ::menu-indicator arrow (never stylesheet that sub-control — it then paints
   nothing and the arrow vanishes). This rule PINS the rest-state box to the
   hover box (same padding + radius, just transparent instead of tinted) so
   the arrow does NOT jump when the tonal container appears on hover (user
   report 2026-07-24: it moved; the hover position is the wanted one). Placed
   BEFORE the :hover / :pressed rules so those win by order on their states
   (equal specificity). InstantPopup replaced the old MenuButtonPopup split
   button: that carved a ::menu-button region which Fusion drew as a raised
   pill and forced 31px of extra width + asymmetric padding — gone with the
   split. */
QToolBar QToolButton[popupMode="2"] {{
    background: transparent;
    border: none;
    padding: 3px 15px;
    border-radius: 6px;
}}

/* Hover / pressed get the SAME rounded container (qt-material paints them as
   a square 12px-bordered slab, which next to the rounded checked state read
   as two different design languages). Weaker tints than checked, so hover
   never looks selected. */
QToolBar QToolButton:hover {{
    background: {hover_tint};
    border: none;
    padding: 3px 15px;
    border-radius: 6px;
}}
QToolBar QToolButton:pressed {{
    background: {press_tint};
    border: none;
    padding: 3px 15px;
    border-radius: 6px;
}}
/* A checked button STAYS looking checked under the pointer (these two rules
   outrank the plain hover/pressed ones above — that is the whole point). */
QToolBar QToolButton:checked:hover {{
    background: {checked_hover_tint};
    border: none;
    padding: 3px 15px;
    border-radius: 6px;
}}
QToolBar QToolButton:checked:pressed {{
    background: {checked_press_tint};
    border: none;
    padding: 3px 15px;
    border-radius: 6px;
}}

/* NOTE: do NOT add a ::menu-indicator rule. The dropdown buttons draw their
   corner arrow through that sub-control, and once a stylesheet touches it
   QStyleSheetStyle takes over the painting — with no `image` declared it
   draws NOTHING, silently deleting the arrow (A/B-rendered 2026-07-23).
   Fusion's default indicator is already a small flat triangle; the box is
   pinned above so it no longer jumps on hover. */

/* Toolbar input fields: ONE height for all of them (qt-material renders
   the size spinbox taller than the font combo beside it). */
QToolBar QComboBox, QToolBar QFontComboBox,
QToolBar QSpinBox, QToolBar QDoubleSpinBox {{
    min-height: 30px;
    max-height: 30px;
}}

/* Page navigation: a quiet "N / total" cluster — the spinbox is a plain
   centred number field (its up/down buttons are gone; the chevrons and
   PgUp/PgDn step pages) with the total reading as its other half.
   qt-material gives every spinbox padding-left: 16px, which shoved the
   centred value right of centre — zero it out. */
QToolBar QSpinBox#page_spin {{ min-width: 44px; padding-left: 0px; padding-right: 0px; }}
QLabel#page_total {{ padding: 0px 8px 0px 2px; color: {tab_fg_dim}; }}

/* Combo dropdowns: ALWAYS a list opening BELOW the control. Fusion's
   default "popup" mode overlays the combo and centres the items (the
   Extract Text scope picker looked broken — user pass 2026-07-04);
   combobox-popup: 0 is the documented QSS lever for list mode, which also
   left-aligns items via the standard item view. App-wide on purpose: every
   combo (scope picker, style toolbar, print preview) behaves the same. */
QComboBox {{ combobox-popup: 0; }}
"""


_mode: str = DARK
_callbacks: list[Callable[[str], None]] = []


def _apply_stylesheet_resilient(app: QApplication, theme_file: str, invert: bool) -> None:
    """``apply_stylesheet`` with a small retry on the concurrent-cache race.

    qt-material regenerates icon SVGs by ``rmtree`` + recreate of a SHARED
    ``~/.qt_material/<parent>`` dir; two processes theming at once can have one
    delete the dir mid-write of the other → ``FileNotFoundError`` (a real crash
    seen when several files were opened at once). The single-instance guard now
    means only the primary themes in the normal flow, so this is just insurance
    for the deliberate multi-window (``PDF_EDITOR_NO_SINGLE_INSTANCE``) case — a
    couple of retries let the racing rebuild finish. A genuinely broken bundle
    still fails: the retries exhaust and re-raise, and the empty-stylesheet
    check in the caller stays loud."""
    attempts = 3
    for i in range(attempts):
        try:
            apply_stylesheet(app, theme=theme_file, invert_secondary=invert)
            return
        except OSError:  # concurrent qt-material cache rebuild — transient
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def apply_theme(app: QApplication, mode: str = DARK) -> None:
    """Apply the qt-material theme for ``mode`` app-wide and notify subscribers.

    Raises RuntimeError if qt-material produced no stylesheet: qt-material
    fails SILENTLY when its theme data files are missing (``get_theme`` logs
    a warning and ``apply_stylesheet`` returns without touching the app) —
    exactly the frozen-build failure the packaging spec guards against.
    Verifying the apply here is what makes the PDF_EDITOR_SMOKE run fail
    loudly (non-zero exit) instead of false-greening on an unthemed app.
    """
    global _mode
    theme_file, invert = _THEMES[mode]  # KeyError on unknown mode is deliberate
    app.setStyleSheet("")  # so the check below sees THIS apply, not residue
    _apply_stylesheet_resilient(app, theme_file, invert)
    if not app.styleSheet():
        raise RuntimeError(
            f"qt-material produced no stylesheet for {theme_file!r} — "
            "theme data files missing from this build?"
        )
    app.setStyleSheet(app.styleSheet() + _qss_addendum(mode))
    _mode = mode
    for callback in list(_callbacks):
        callback(mode)


def current_mode() -> str:
    """The mode last applied ("dark" until apply_theme says otherwise)."""
    return _mode


def accent() -> str:
    """Accent colour hex — follows the applied qt-material theme's primary."""
    return os.environ.get("QTMATERIAL_PRIMARYCOLOR") or _FALLBACK_ACCENT


def canvas_brush(mode: str | None = None) -> QBrush:
    """Backdrop brush behind the rendered page (the page pixmap stays white)."""
    return QBrush(QColor(_CANVAS[_mode if mode is None else mode]))


def armed_chip_qss() -> str:
    """Widget-local style for the canvas armed-mode chip (U4).

    Built at show time with the CURRENT mode (chips are short-lived, like
    the S5 dialogs); the canvas re-applies it on a theme switch while armed.
    """
    dark = _mode == DARK
    bg = "#2b2f33" if dark else "#ffffff"
    fg = "#e0e0e0" if dark else "#37474f"
    return (
        f"QLabel#armed_chip {{ background-color: {bg}; color: {fg};"
        f" border: 1px solid {accent()}; border-radius: 4px; padding: 4px 10px; }}"
    )


def on_change(callback: Callable[[str], None]) -> None:
    """Register ``callback(mode)`` to run after every apply_theme()."""
    _callbacks.append(callback)


def _reset_for_tests() -> None:
    """Restore module state (tests share one session QApplication)."""
    global _mode
    _mode = DARK
    _callbacks.clear()
    for key in [k for k in os.environ if k.startswith("QTMATERIAL_")]:
        del os.environ[key]
