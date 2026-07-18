"""Offscreen tests for armed-mode visibility (U4).

Arming a one-shot mode shows a persistent chip on the canvas (Esc cancels);
the launching toolbar action reads checked while armed and clicking it again
cancels. Esc handles the armed mode before touching the selection.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402


def _scene_point(view, px, py):
    return page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )


def test_arming_shows_chip_and_click_disarms_and_hides(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.begin_insert_text()
        chip = view._canvas._armed_chip
        assert not chip.isHidden()
        assert "Esc cancels" in chip.text()

        z = view._canvas.render_zoom
        view._canvas.insertPointSelected.emit(300 * z, 400 * z)  # one-shot fires
        view._canvas.disarm_insert_point()  # the real click path disarms first
        assert chip.isHidden()
        view._para_editor.cancel()
    finally:
        window.close()


def test_escape_cancels_armed_mode_before_selection(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        view._on_select_drag_started(*_scene_point(view, cx, cy))
        assert view._selection is not None

        view.begin_highlight()
        assert view.armed_action == "highlight"
        view._on_escape()  # first Esc: disarm, keep the selection
        assert view.armed_action is None
        assert view._canvas._armed_chip.isHidden()
        assert view._selection is not None

        view._on_escape()  # second Esc: deselect
        assert view._selection is None
    finally:
        window.close()


def test_toolbar_has_insert_buttons_not_mode_toggles(qapp):
    """E11.4 (user request): Insert text/image are toolbar buttons; the
    reveal-all and double-click sub-mode toggles are menu-only now."""
    from PySide6.QtWidgets import QToolBar

    from pdfapp.main_window import MainWindow

    window = MainWindow()
    try:
        toolbars = window.findChildren(QToolBar)
        toolbar_actions = {a for tb in toolbars for a in tb.actions()}
        assert window._insert_text_action in toolbar_actions
        assert window._insert_image_action in toolbar_actions
        assert window._show_areas_action not in toolbar_actions
        assert window._dblclick_para_action not in toolbar_actions
        # ...but both toggles remain reachable in the Edit menu.
        menu_actions = set(window.menuBar().actions())
        edit_menu = next(a.menu() for a in menu_actions if a.text() == "&Edit")
        assert window._show_areas_action in edit_menu.actions()
        assert window._dblclick_para_action in edit_menu.actions()
    finally:
        window.close()


def test_toolbar_action_checks_while_armed_and_cancels_on_reclick(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        window.active_view.set_edit_mode(True)
        window.insert_text()
        assert window.active_view.armed_action == "text"
        assert window._insert_text_action.isChecked()
        assert not window._highlight_action.isChecked()

        window.insert_text()  # clicking the checked action cancels the mode
        assert window.active_view.armed_action is None
        assert not window._insert_text_action.isChecked()
        assert window.active_view._canvas._armed_chip.isHidden()
    finally:
        window.close()


def test_cancelled_image_dialog_leaves_action_unchecked(qapp, quote_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        monkeypatch.setattr(view, "_prompt_image_path", lambda: None)
        window.insert_image()
        assert view.armed_action is None
        assert not window._insert_image_action.isChecked()
        assert view._canvas._armed_chip.isHidden()
    finally:
        window.close()


def test_leaving_edit_mode_drops_chip_and_armed_state(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.begin_insert_text()
        assert not view._canvas._armed_chip.isHidden()
        view.set_edit_mode(False)
        assert view.armed_action is None
        assert view._canvas._armed_chip.isHidden()
    finally:
        window.close()


def test_no_chip_in_read_only(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.begin_insert_text()  # gated by U0
        assert view._canvas._armed_chip.isHidden()
    finally:
        window.close()
