"""Tests for the central theme module (restyle S1).

The shared ``theme_app`` fixture (conftest) restores everything qt-material
mutates on the session QApplication — stylesheet, app font, palette, QStyle
— plus the theme module's state.
"""

from __future__ import annotations

import pytest


def test_apply_dark_sets_stylesheet_with_addendum(theme_app):
    app, theme = theme_app
    theme.apply_theme(app)
    assert len(app.styleSheet()) > 1000  # qt-material QSS is large
    assert "pdf-editor-addendum" in app.styleSheet()  # our layer survived
    # The polish rules ride the addendum: tab-strip band + uniform toolbar
    # input heights + the page-nav cluster.
    assert "QTabBar::tab:selected" in app.styleSheet()
    assert "QToolBar QSpinBox#page_spin" in app.styleSheet()
    assert "QToolButton:checked" in app.styleSheet()  # tonal fill, not side-bar
    assert theme.current_mode() == theme.DARK


def test_light_mode_applies_a_different_stylesheet(theme_app):
    app, theme = theme_app
    theme.apply_theme(app, theme.DARK)
    dark_qss = app.styleSheet()
    theme.apply_theme(app, theme.LIGHT)
    assert theme.current_mode() == theme.LIGHT
    assert app.styleSheet() != dark_qss
    assert "pdf-editor-addendum" in app.styleSheet()  # re-appended on re-apply


def test_unknown_mode_is_rejected_before_any_styling(theme_app):
    app, theme = theme_app
    with pytest.raises(KeyError):
        theme.apply_theme(app, "solarized")
    assert app.styleSheet() == ""  # nothing half-applied


def test_apply_theme_retries_the_concurrent_cache_race(theme_app, monkeypatch):
    """qt-material rebuilds a SHARED on-disk icon cache (rmtree + recreate); two
    processes theming at once can have one delete it mid-write of the other,
    raising OSError (the reported multi-open crash). apply_theme retries so the
    transient race doesn't take the app down."""
    import pdfapp.theme as theme_mod

    app, theme = theme_app
    real = theme_mod.apply_stylesheet
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # first attempt hits the race
            raise FileNotFoundError("~/.qt_material/theme/active/branch-closed.svg")
        return real(*args, **kwargs)

    monkeypatch.setattr(theme_mod, "apply_stylesheet", flaky)
    theme.apply_theme(app)  # must NOT propagate the transient error
    assert calls["n"] == 2  # failed once, retried, succeeded
    assert "pdf-editor-addendum" in app.styleSheet()


def test_apply_theme_reraises_a_persistent_error(theme_app, monkeypatch):
    """A genuinely broken bundle (not a transient race) still fails loudly after
    the retries exhaust — the frozen-build smoke must not be masked."""
    import pdfapp.theme as theme_mod

    app, theme = theme_app

    def always_fails(*args, **kwargs):
        raise FileNotFoundError("missing source svgs")

    monkeypatch.setattr(theme_mod, "apply_stylesheet", always_fails)
    with pytest.raises(OSError):
        theme.apply_theme(app)


def test_canvas_brush_differs_per_mode_and_follows_current(theme_app):
    app, theme = theme_app
    assert theme.canvas_brush(theme.DARK) != theme.canvas_brush(theme.LIGHT)
    theme.apply_theme(app, theme.LIGHT)
    assert theme.canvas_brush() == theme.canvas_brush(theme.LIGHT)


def test_accent_falls_back_then_follows_theme(theme_app):
    app, theme = theme_app
    assert theme.accent() == "#4a90d9"  # before any apply (env cleared by fixture)
    theme.apply_theme(app)
    accent = theme.accent()
    assert accent.startswith("#") and len(accent) == 7


def test_on_change_callbacks_fire_with_the_new_mode(theme_app):
    app, theme = theme_app
    seen: list[str] = []
    theme.on_change(seen.append)
    theme.apply_theme(app, theme.LIGHT)
    theme.apply_theme(app, theme.DARK)
    assert seen == [theme.LIGHT, theme.DARK]


# --- S3: runtime dark/light toggle ----------------------------------------


def test_view_menu_dark_toggle_switches_theme_and_rebrushes(theme_app, text_pdf):
    from pdfapp.main_window import MainWindow

    app, theme = theme_app
    window = MainWindow()
    window.open_path(text_pdf)
    assert window._dark_theme_action.isChecked()  # dark is the launch default

    window._dark_theme_action.setChecked(False)  # user picks light
    assert theme.current_mode() == theme.LIGHT
    assert "pdf-editor-addendum" in app.styleSheet()
    view = window.active_view
    assert view._canvas.backgroundBrush() == theme.canvas_brush(theme.LIGHT)

    window._dark_theme_action.setChecked(True)  # and back
    assert theme.current_mode() == theme.DARK
    assert view._canvas.backgroundBrush() == theme.canvas_brush(theme.DARK)
    window.close()


# --- S2: the two reconciled chrome spots ---------------------------------


def test_overlay_bakes_base_font_into_widget_stylesheet(qapp):
    # qt-material's global font-family rule outranks setFont(); the matched
    # span font must ride in the widget-level stylesheet or editors render
    # the theme font over the page.
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QFont

    from pdfapp.text_editor_overlay import TextEditorOverlay

    overlay = TextEditorOverlay()
    font = QFont("Georgia")
    font.setPixelSize(23)
    font.setBold(True)
    overlay.open_at(QRect(0, 0, 120, 30), "value", font)
    qss = overlay.styleSheet()
    assert "background: white" in qss  # light chrome is load-bearing
    assert 'font-family: "Georgia"' in qss
    assert "font-size: 23px" in qss
    assert "font-weight: bold" in qss
    overlay.cancel()


def test_overlay_chrome_uses_theme_accent(qapp):
    from pdfapp import theme
    from pdfapp.text_editor_overlay import TextEditorOverlay

    overlay = TextEditorOverlay()
    assert f"border: 1px solid {theme.accent()}" in overlay.styleSheet()


def test_canvas_backdrop_comes_from_theme(qapp):
    from pdfapp import theme
    from pdfapp.page_canvas import PageCanvas

    canvas = PageCanvas()
    assert canvas.backgroundBrush() == theme.canvas_brush()


def test_silent_qt_material_noop_raises_instead_of_false_success(theme_app, monkeypatch):
    # qt-material returns WITHOUT touching the stylesheet when its theme data
    # files are missing (the frozen-build failure) — apply_theme must raise,
    # not report success, or the packaging smoke false-greens.
    app, theme = theme_app
    monkeypatch.setattr(theme, "apply_stylesheet", lambda *a, **k: None)
    seen: list[str] = []
    theme.on_change(seen.append)
    with pytest.raises(RuntimeError, match="theme data files"):
        theme.apply_theme(app, theme.LIGHT)
    assert theme.current_mode() == theme.DARK  # no half-applied state
    assert seen == []  # subscribers never told of a failed apply
