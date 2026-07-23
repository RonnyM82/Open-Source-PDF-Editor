"""Offscreen tests for the justification control on the text-style toolbar.

One button wearing the ACTIVE option, a dropdown carrot for the others, and a
STICKY (persisted) last-used choice — user request. The autouse
`_isolate_app_data` fixture (conftest) keeps the persisted value out of the
developer's real profile.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QToolBar  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402


def _paragraph_pdf(tmp_path):
    import pymupdf

    path = tmp_path / "para.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "HEADER row", fontname="hebo", fontsize=8)
    page.insert_text((72, 111), "make two words bold here", fontsize=8)
    page.insert_text((72, 119), "second body line of text", fontsize=8)
    page.insert_text((72, 127), "third body line to finish", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def _right_aligned_pdf(tmp_path):
    """Two lines sharing a right edge at x=300 (a quote's totals labels)."""
    import pymupdf

    path = tmp_path / "right.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(("Subtotal ex GST", "Shipping Cost (UPS)")):
        width = pymupdf.get_text_length(line, fontname="helv", fontsize=9)
        page.insert_text((300 - width, 200 + i * 11), line, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def test_align_button_shows_the_active_option_with_the_others_in_its_menu(qapp):
    window = MainWindow()
    try:
        assert set(window._align_actions) == {"left", "center", "right"}
        assert window._text_align == "left"  # first-launch default
        assert window._align_actions["left"].isChecked()
        assert not window._align_button.icon().isNull()
        assert window._align_button.toolTip()
        # Every option is reachable from the ONE button's dropdown. It follows
        # the highlighter swatch's InstantPopup pattern (a click opens the
        # list; no MenuButtonPopup split region, which was 31px wider and
        # rendered a raised pill — user reports 2026-07-23/24).
        assert window._align_button.menu() is not None
        assert len(window._align_button.menu().actions()) == 3
        assert (
            window._align_button.popupMode()
            == window._align_button.ToolButtonPopupMode.InstantPopup
        )
        for action in window._align_actions.values():
            assert not action.icon().isNull()
            assert action.toolTip()
    finally:
        window.close()


def test_align_button_lives_on_the_style_toolbar_and_keeps_nofocus(qapp):
    """Like every style control, it must not steal focus from an open editor."""
    window = MainWindow()
    try:
        bar = window.findChild(QToolBar, "text_style_toolbar")
        assert window._align_button.parent() is bar
        assert window._align_button.focusPolicy() == Qt.FocusPolicy.NoFocus
    finally:
        window.close()


def test_dropdown_buttons_are_no_wider_than_a_plain_icon_button(qapp):
    """User report (2026-07-24): the alignment button took up a lot more space
    than the others — the MenuButtonPopup split reserved an extra arrow
    region. As a flat InstantPopup button it matches a plain icon button, like
    the highlighter swatch already does."""
    window = MainWindow()
    try:
        window.resize(1500, 700)
        window.show()
        qapp.processEvents()
        style_bar = window.findChild(QToolBar, "text_style_toolbar")
        bold_w = style_bar.widgetForAction(window._bold_action).width()
        assert window._align_button.width() == bold_w
        assert window._highlight_color_button.width() == bold_w
    finally:
        window.close()


def test_picking_an_alignment_makes_it_the_active_button(qapp):
    window = MainWindow()
    try:
        window._pick_text_align("right")
        assert window.current_text_align() == "right"
        assert window._align_actions["right"].isChecked()
        assert not window._align_actions["left"].isChecked()
        icon = window._align_button.icon().pixmap(20, 20).toImage()
        assert icon == window._align_actions["right"].icon().pixmap(20, 20).toImage()
    finally:
        window.close()


def test_last_used_alignment_persists_to_the_next_window(qapp):
    w1 = MainWindow()
    try:
        w1._pick_text_align("center")
        assert w1._settings.get("last_text_align") == "center"
    finally:
        w1.close()
    w2 = MainWindow()
    try:
        assert w2.current_text_align() == "center"  # the button starts there
        assert w2._align_actions["center"].isChecked()
    finally:
        w2.close()


def test_corrupt_persisted_alignment_falls_back_to_left(qapp):
    window = MainWindow()
    try:
        window._settings.set("last_text_align", "justified-ish")
        assert window._startup_text_align() == "left"
    finally:
        window.close()


def test_align_icons_rebake_when_the_theme_changes(theme_app):
    app, theme = theme_app
    window = MainWindow()
    try:
        dark = window._align_button.icon().pixmap(20, 20).toImage()
        theme.apply_theme(app, theme.LIGHT)
        assert window._align_button.icon().pixmap(20, 20).toImage() != dark
    finally:
        window.close()


def _insert_at(view, px, py):
    """Arm-free click-to-place: the canvas reports SCENE pixels."""
    from pdfapp import page_coords

    sx, sy = page_coords.page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )
    view._click_action = ("text", None)
    view._on_insert_point(sx, sy)


def test_insert_uses_the_toolbar_alignment(qapp, tmp_path):
    """A multi-line insert lines its shorter lines up on the widest one."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        window._pick_text_align("right")

        _insert_at(view, 100.0, 300.0)
        view._para_editor.setPlainText("a much longer inserted line\nshort\nmid one")
        view._para_editor.commit()

        spans = [
            s
            for s in view.document.text_spans(0)
            if s.text.strip() in ("a much longer inserted line", "short", "mid one")
        ]
        assert len(spans) == 3
        rights = [s.bbox[2] for s in spans]
        assert max(rights) - min(rights) < 1.0  # justified right
    finally:
        window.close()


def test_paragraph_edit_applies_the_picked_alignment(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, 119)
        assert para.align == "left"

        view._begin_paragraph_edit(0, para)
        window._pick_text_align("right")  # user re-justifies mid-edit
        view._para_editor.commit()  # text untouched — alignment IS the change

        spans = [
            s
            for s in view.document.text_spans(0)
            if s.text.strip().startswith(("make", "second", "third"))
        ]
        assert len(spans) == 3
        rights = [s.bbox[2] for s in spans]
        assert max(rights) - min(rights) < 1.0
    finally:
        window.close()


def test_alignment_only_change_is_not_a_no_op(qapp, tmp_path):
    """Committing unchanged TEXT with a changed alignment must still push a
    command (the no-op check has to know about justification)."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view._begin_paragraph_edit(0, view.document.paragraph_at(0, 100, 119))
        window._pick_text_align("center")
        view._para_editor.commit()
        assert view.undo_stack.count() == 1
    finally:
        window.close()


def test_unchanged_paragraph_commit_stays_a_no_op(qapp, tmp_path):
    """The flip side: open and commit with nothing touched — the reflected
    alignment matches what the paragraph already is, so nothing is pushed."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view._begin_paragraph_edit(0, view.document.paragraph_at(0, 100, 119))
        view._para_editor.commit()
        assert view.undo_stack.count() == 0
    finally:
        window.close()


def test_opening_a_right_aligned_paragraph_reflects_it_without_bleeding(qapp, tmp_path):
    """Reflection is display-only (the E11.3 independence rule): the button
    shows the clicked paragraph's justification while editing, and the user's
    last-used option comes back when the editor closes."""
    window = MainWindow()
    try:
        window._pick_text_align("left")  # the global default
        window.open_path(_right_aligned_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 250, 199)
        assert para is not None and para.align == "right"

        view._begin_paragraph_edit(0, para)
        assert window.current_text_align() == "right"  # reflected...
        assert window._global_style["align"] == "left"  # ...but not captured
        assert window._settings.get("last_text_align") == "left"

        view._para_editor.cancel()
        assert window.current_text_align() == "left"  # global restored
    finally:
        window.close()


def test_editing_a_right_aligned_paragraph_keeps_it_right(qapp, tmp_path):
    """The reflected value is what the commit applies — an edit must not
    re-justify a right-aligned block to the global default."""
    window = MainWindow()
    try:
        window._pick_text_align("left")
        window.open_path(_right_aligned_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 250, 199)

        view._begin_paragraph_edit(0, para)
        view._para_editor.setPlainText("Subtotal excluding GST\nShipping Cost (UPS)")
        view._para_editor.commit()

        spans = [s for s in view.document.text_spans(0) if "Sub" in s.text or "Shipping" in s.text]
        assert len(spans) == 2
        assert max(s.bbox[2] for s in spans) - min(s.bbox[2] for s in spans) < 1.5
    finally:
        window.close()


def test_pick_justifies_the_open_editor_live(qapp, tmp_path):
    """The editor shows the layout the commit will produce."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view._begin_paragraph_edit(0, view.document.paragraph_at(0, 100, 119))

        window._pick_text_align("center")
        block = view._para_editor.document().begin()
        while block.isValid():
            assert block.blockFormat().alignment() == Qt.AlignmentFlag.AlignHCenter
            block = block.next()
    finally:
        window.close()


def test_span_editor_is_left_alone_by_a_pick(qapp, tmp_path):
    """A single span has nothing to justify and its commit ignores alignment,
    so the pick must not restyle that editor (it would show a change that
    never lands on the page)."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if "second body" in s.text)
        view._begin_text_edit(0, span)

        assert view.apply_alignment_to_editor("right") is False
        assert view._editor.is_editing  # the edit survives untouched
    finally:
        window.close()
