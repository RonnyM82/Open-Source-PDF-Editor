"""Offscreen tests for the list controls on the Text style toolbar (list v2).

Two mutually-exclusive Bulleted/Numbered TOGGLES plus Increase/Decrease
indent — the Acrobat Format-panel model that replaced v1's Insert-list
command and sticky List-style dropdown. The toggles track the caret's block
through the selectionFormatChanged flow like B/I/U, act on the open editor,
and fall back to a one-shot command on a selected text box.
"""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QToolBar  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfcore.textedit import list_item_kind  # noqa: E402


def _para_pdf(tmp_path, text="A paragraph to format as a list"):
    path = tmp_path / "p.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), text, fontname="helv", fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _edit_view(window, tmp_path, **kw):
    window.open_path(_para_pdf(tmp_path, **kw))
    view = window.active_view
    view.set_edit_mode(True)
    view._canvas.resize(800, 600)
    return view


def _geom_para(view, needle):
    return next(p for p in view.page_geometry(0).paragraphs if needle in p.text)


def test_list_controls_live_on_the_style_toolbar(qapp):
    window = MainWindow()
    try:
        bar = window.findChild(QToolBar, "text_style_toolbar")
        names = [a.text() for a in bar.actions()]
        assert "Bulleted list" in names and "Numbered list" in names
        assert "Increase indent" in names and "Decrease indent" in names
        assert window._list_bullet_action.isCheckable()
        assert window._list_number_action.isCheckable()
        assert not window._list_bullet_action.icon().isNull()
        assert not window._indent_more_action.icon().isNull()
        # The old v1 controls are gone: no Insert-list command, no dropdown.
        assert not hasattr(window, "_insert_list_action")
        assert not hasattr(window, "_list_kind_button")
    finally:
        window.close()


def test_toggle_formats_the_open_editor(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _edit_view(window, tmp_path)
        view._begin_paragraph_edit(0, _geom_para(view, "format as a list"))
        window._toggle_list("bullet")
        assert view._para_editor.caret_list_state() == ("bullet", 0)
        assert window._list_bullet_action.isChecked()
        assert not window._list_number_action.isChecked()
        view._para_editor.commit()
        assert view.undo_stack.count() == 1
    finally:
        window.close()


def test_toggles_track_the_caret_via_selection_flow(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _edit_view(window, tmp_path)
        view._begin_paragraph_edit(0, _geom_para(view, "format as a list"))
        view.toggle_editor_list("number")
        view._on_editor_selection_changed()  # the caret-tracking flow
        assert window._list_number_action.isChecked()
        assert not window._list_bullet_action.isChecked()
        view._para_editor.cancel()
        window._on_editor_closed()
        assert not window._list_number_action.isChecked()  # session over
    finally:
        window.close()


def test_toggle_with_no_editor_formats_the_selected_box(qapp, tmp_path):
    """The one-shot path: select a text box, click the toggle."""
    window = MainWindow()
    try:
        view = _edit_view(window, tmp_path)
        para = _geom_para(view, "format as a list")
        view._selection = ("text", 0, para)
        window._toggle_list("number")
        assert view.undo_stack.count() == 1
        after = _geom_para(view, "format as a list")
        assert list_item_kind(after)[0] == "number"
        # Clicking the SAME kind on the (re-selected) item removes it.
        view._selection = ("text", 0, after)
        window._toggle_list("number")
        assert view.undo_stack.count() == 2
        assert list_item_kind(_geom_para(view, "format as a list"))[0] is None
    finally:
        window.close()


def test_indent_buttons_step_the_selected_item(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _edit_view(window, tmp_path)
        para = _geom_para(view, "format as a list")
        view._selection = ("text", 0, para)
        window._toggle_list("bullet")
        item = _geom_para(view, "format as a list")
        left0 = item.bbox[0]
        view._selection = ("text", 0, item)
        window._indent_list(+1)
        indented = _geom_para(view, "format as a list")
        assert indented.bbox[0] == pytest.approx(left0 + 18.0, abs=1.5)
    finally:
        window.close()


def test_list_toggle_is_inert_in_markup_mode(qapp, tmp_path):
    """Content edits stay edit-mode gated: the toggle does nothing in Markup
    mode (the view refuses; no command lands)."""
    window = MainWindow()
    try:
        window.open_path(_para_pdf(tmp_path))
        view = window.active_view  # markup mode (default)
        para = next(p for p in view.page_geometry(0).paragraphs)
        view._selection = ("text", 0, para)
        window._toggle_list("bullet")
        assert view.undo_stack.count() == 0
    finally:
        window.close()


def test_list_icons_rebake_when_the_theme_changes(theme_app):
    app, theme = theme_app
    window = MainWindow()
    try:
        dark = window._list_bullet_action.icon().pixmap(20, 20).toImage()
        theme.apply_theme(app, theme.LIGHT)
        assert window._list_bullet_action.icon().pixmap(20, 20).toImage() != dark
    finally:
        window.close()
