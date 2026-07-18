"""Offscreen tests for the text-style toolbar (E5.4)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402


def _page_text(view) -> str:
    return view.document._doc[0].get_text()


def _find_span(view, text):
    return next(s for s in view.document.text_spans(0) if s.text.strip() == text)


def test_font_choice_maps_base14_families(qapp):
    # Pure mapping (the offscreen font database may resolve combo families
    # unpredictably, so the mapping is tested directly).
    from pdfapp.main_window import _font_choice

    assert _font_choice("Arial", False) == ("helv", None, True)
    assert _font_choice("Arial", True) == ("hebo", None, True)
    assert _font_choice("Helvetica", False) == ("helv", None, True)
    assert _font_choice("Times New Roman", False) == ("tiro", None, True)
    assert _font_choice("Times New Roman", True) == ("tibo", None, True)
    assert _font_choice("Courier New", False) == ("cour", None, True)

    code, fontfile, resolved = _font_choice("NoSuchFamilyXYZ123", False)
    assert (code, fontfile, resolved) == ("helv", None, False)  # honest fallback

    # Italic flows through (review finding: it was silently dropped).
    assert _font_choice("Arial", False, italic=True) == ("heit", None, True)
    assert _font_choice("Arial", True, italic=True) == ("hebi", None, True)
    assert _font_choice("Times New Roman", False, italic=True) == ("tiit", None, True)


def test_populate_reflects_italic_and_commit_keeps_it(qapp, tmp_path):
    """Review finding: editing a Helvetica-Oblique span re-inserted it
    upright with no warning. Populate must set the Italic toggle and the
    committed style must map back to the italic code."""
    import pymupdf

    path = tmp_path / "italic.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "slanted words here", fontname="heit", fontsize=10)
    doc.save(str(path))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        span = next(s for s in view.document.text_spans(0) if "slanted" in s.text)
        assert span.base14 == "heit"
        view._begin_text_edit(0, span)
        assert window._italic_action.isChecked()

        view._editor.setText("slanted words fixed")
        view._editor._commit()
        new_span = next(s for s in view.document.text_spans(0) if "slanted" in s.text)
        assert new_span.base14 == "heit"  # italic survived the round-trip
    finally:
        window.close()


def test_font_choice_system_font_resolves_to_file(qapp):
    from pdfapp.main_window import _font_choice

    code, fontfile, resolved = _font_choice("Verdana", False)
    if fontfile is None:
        pytest.skip("Verdana not installed on this machine")
    assert resolved
    assert fontfile.lower().endswith((".ttf", ".otf"))


def test_toolbar_state_flows_into_style(qapp):
    window = MainWindow()
    try:
        window._size_spin.setValue(15.5)
        window._bold_action.setChecked(True)
        window._underline_action.setChecked(True)
        window._text_color = QColor(0, 128, 0)
        style, preview = window.current_text_style()
        assert style.size == pytest.approx(15.5)
        assert style.underline
        assert style.color == 0x008000
        assert preview.bold() and preview.underline()
    finally:
        window.close()


def test_toolbar_scripts_are_mutually_exclusive(qapp):
    window = MainWindow()
    try:
        window._super_action.setChecked(True)
        window._sub_action.setChecked(True)
        assert not window._super_action.isChecked()
        window._super_action.setChecked(True)
        assert not window._sub_action.isChecked()
    finally:
        window.close()


def test_opening_editor_populates_toolbar(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        heading = _find_span(view, quote_pdf.heading)  # hebo, 16pt
        view._begin_text_edit(0, heading)
        assert window._bold_action.isChecked()
        assert window._size_spin.value() == pytest.approx(16.0, abs=0.1)

        red = _find_span(view, quote_pdf.red_text)  # helv, colour (1,0,0)
        view._begin_text_edit(0, red)
        assert not window._bold_action.isChecked()
        assert window._text_color == QColor(255, 0, 0)
    finally:
        window.close()


def test_commit_applies_toolbar_style(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        span = _find_span(view, quote_pdf.price)
        view._begin_text_edit(0, span)  # populates toolbar from the span

        window._size_spin.setValue(14.0)  # user bumps the size
        view._editor.setText("$3.33")
        view._editor._commit()

        new_span = _find_span(view, "$3.33")
        assert new_span.size == pytest.approx(14.0, abs=0.1)
    finally:
        window.close()


def test_restyle_existing_text_applies(qapp, quote_pdf):
    """The core Bug-A repro: double-click an EXISTING plain span, turn on Bold
    in the toolbar mid-edit, commit — the text becomes bold. Previously the
    toolbar click stole focus and cancelled the edit, so nothing applied."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        span = _find_span(view, quote_pdf.price)  # helv, not bold
        assert span.base14 == "helv"

        view._begin_text_edit(0, span)
        assert view._editor.is_editing
        window._bold_action.setChecked(True)  # user clicks Bold mid-edit
        assert view._editor.is_editing  # edit survives the toolbar interaction

        view._editor.commit()  # unchanged text, but now bold
        restyled = _find_span(view, quote_pdf.price)
        assert restyled.base14 == "hebo"  # bold applied to existing text
    finally:
        window.close()


def test_style_toolbar_focus_policies(qapp):
    """Mouse-driven controls stay NoFocus (the editor keeps focus so Enter
    commits). The size SPIN is the deliberate exception (user request): it
    takes focus on click so a size can be TYPED, and editingFinished hands
    focus back to the open editor."""
    window = MainWindow()
    try:
        from PySide6.QtCore import Qt

        for widget in (window._font_combo, window._color_button):
            assert widget.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert window._size_spin.focusPolicy() == Qt.FocusPolicy.ClickFocus
    finally:
        window.close()


# --- selection-level styling (E9) -------------------------------------------


def _select(editor, needle: str) -> None:
    from PySide6.QtGui import QTextCursor

    text = editor.toPlainText()
    start = text.index(needle)
    cursor = editor.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(start + len(needle), QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)


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


def test_size_field_tracks_selection_blank_when_mixed(qapp, tmp_path):
    """User request: selecting text of MIXED sizes blanks the size field;
    selecting a uniform run shows its ACTUAL size."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, 112)
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor

        _select(editor, "two words")
        view.apply_size_pt_to_editor(14.0)  # now the block has 8pt AND 14pt

        _select(editor, "words bold")  # 14pt "words" + 8pt "bold": mixed
        assert window._size_spin.text() == ""  # field goes BLANK

        _select(editor, "two words")  # uniform 14pt stretch
        assert window._size_spin.value() == pytest.approx(14.0)

        _select(editor, "here")  # untouched 8pt stretch
        assert window._size_spin.value() == pytest.approx(8.0)
    finally:
        window.close()


def test_enlarged_text_grows_the_editor_not_clipped(qapp, tmp_path):
    """User report: enlarging a word cropped it at the box edge — the block's
    PINNED line height stayed at the original pitch and the box never grew.
    The pin now scales with the tallest fragment and the box follows."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, 112)
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor
        height_before = editor.height()
        pinned_before = editor.document().begin().blockFormat().lineHeight()

        _select(editor, "two words")
        view.apply_size_pt_to_editor(30.0)

        first_block = editor.document().begin()
        assert first_block.blockFormat().lineHeight() > pinned_before + 1  # line got room
        assert editor.height() > height_before  # ...and the box grew to show it
    finally:
        window.close()


def test_enlarged_text_grows_single_line_editor_too(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        view._begin_text_edit(0, span)
        editor = view._editor
        height_before = editor.height()

        editor.selectAll()
        view.apply_size_pt_to_editor(30.0)
        assert editor.height() > height_before  # tall text no longer clipped
    finally:
        window.close()


def test_bold_italic_underline_have_shortcuts(qapp):
    from PySide6.QtGui import QKeySequence

    window = MainWindow()
    try:
        assert window._bold_action.shortcut() == QKeySequence(QKeySequence.StandardKey.Bold)
        assert window._italic_action.shortcut() == QKeySequence(QKeySequence.StandardKey.Italic)
        assert window._underline_action.shortcut() == QKeySequence(
            QKeySequence.StandardKey.Underline
        )
    finally:
        window.close()


def test_toggles_track_selection_and_mixed_unchecks(qapp, tmp_path):
    """E11.3 (user request): the B/I/U toggle states react to the SELECTED
    text — checked for a uniformly-styled selection, unchecked for mixed."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, 112)
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor

        _select(editor, "two words")
        window._bold_action.setChecked(True)  # bold just those words

        _select(editor, "two words")  # uniformly bold selection
        assert window._bold_action.isChecked()

        _select(editor, "make two")  # plain "make " + bold "two": mixed
        assert not window._bold_action.isChecked()

        _select(editor, "bold here")  # untouched plain stretch
        assert not window._bold_action.isChecked()
    finally:
        window.close()


def test_selection_reflection_does_not_bleed_into_global_defaults(qapp, tmp_path):
    """E11.3 (user request): the toggle state while editing is INDEPENDENT of
    the global insert defaults — closing the editor restores them."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)

        # Global default: bold ON (set with no editor open).
        window._bold_action.setChecked(True)
        assert window._global_style["bold"] is True

        # Open an editor on plain text: reflection UNchecks the button...
        para = view.document.paragraph_at(0, 100, 119)
        view._begin_paragraph_edit(0, para)
        _select(view._para_editor, "second body line")
        assert not window._bold_action.isChecked()
        # ...but the global default is untouched.
        assert window._global_style["bold"] is True

        # Closing the editor restores the global state to the controls.
        view._para_editor.cancel()
        assert window._bold_action.isChecked()
    finally:
        window.close()


def test_script_toggles_track_selection_and_mixed_unchecks(qapp, tmp_path):
    """User request (2026-07-18): the super/subscript buttons follow the same
    selection-tracking rules as B/I/U — checked for a uniformly-scripted
    selection, unchecked for mixed or plain."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, 112)
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor

        _select(editor, "two words")
        window._super_action.setChecked(True)  # superscript just those words

        _select(editor, "make two")  # plain + superscript: mixed
        assert not window._super_action.isChecked()
        assert not window._sub_action.isChecked()

        _select(editor, "two words")  # uniformly superscript (real reflection
        assert window._super_action.isChecked()  # — the range changed above)
        assert not window._sub_action.isChecked()

        _select(editor, "bold here")  # untouched plain stretch
        assert not window._super_action.isChecked()
    finally:
        window.close()


def test_colour_swatch_tracks_selection_and_mixed_neutralises(qapp, tmp_path):
    """User request (2026-07-18): the colour swatch follows the selection — a
    uniform colour shows in the swatch (and seeds the picker), a mixed
    selection shows the neutral crossed swatch instead."""
    from PySide6.QtGui import QTextCharFormat

    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        para = view.document.paragraph_at(0, 100, 112)
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor

        _select(editor, "two words")
        red = QColor(200, 30, 30)
        fmt = QTextCharFormat()
        fmt.setForeground(red)
        window._apply_to_selection(fmt)  # what _pick_text_color does, sans dialog

        # (Re-selecting the SAME range fires no selection signal — move away
        # first; every assertion below follows a real selection change.)
        _select(editor, "make two")  # black + red: mixed -> neutral swatch
        assert window._color_swatch_mixed

        _select(editor, "two words")  # uniformly red
        assert window._text_color == red
        assert not window._color_swatch_mixed

        _select(editor, "bold here")  # uniform black stretch
        assert not window._color_swatch_mixed
        assert window._text_color == QColor(0, 0, 0)
    finally:
        window.close()


def test_script_and_colour_reflection_do_not_bleed_into_globals(qapp, tmp_path):
    """User request (2026-07-18): like B/I/U, script + colour reflection is
    display-only — the GLOBAL insert defaults survive an editor session and
    restore to the controls when it ends."""
    from pdfcore.textedit import SCRIPT_SUPER

    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)

        # Global defaults chosen with no editor open: superscript + red.
        red = QColor(180, 20, 20)
        window._super_action.setChecked(True)
        window._text_color = QColor(red)
        window._update_color_swatch()
        window._maybe_capture_global_style()  # the picker's capture path
        assert window._global_style["script"] == SCRIPT_SUPER
        assert window._global_style["color"] == red

        # Reflection over plain black text unchecks/repaints the controls...
        para = view.document.paragraph_at(0, 100, 119)
        view._begin_paragraph_edit(0, para)
        _select(view._para_editor, "second body line")
        assert not window._super_action.isChecked()
        assert window._text_color == QColor(0, 0, 0)
        # ...but the globals are untouched.
        assert window._global_style["script"] == SCRIPT_SUPER
        assert window._global_style["color"] == red

        # Closing the editor restores the global state to the controls.
        view._para_editor.cancel()
        assert window._super_action.isChecked()
        assert not window._sub_action.isChecked()
        assert window._text_color == red
        assert not window._color_swatch_mixed
    finally:
        window.close()


def test_make_just_two_words_bold(qapp, tmp_path):
    """THE core E9 ask: select two words mid-paragraph, click Bold, commit —
    only those words come back bold."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        para = view.document.paragraph_at(0, 100, 112)
        view._begin_paragraph_edit(0, para)

        _select(view._para_editor, "two words")
        window._bold_action.setChecked(True)  # toolbar formats the selection
        view._para_editor.commit()

        spans = view.document.text_spans(0)
        bolded = next(s for s in spans if s.text.strip() == "two words")
        assert bolded.font == "Helvetica-Bold"
        before = next(s for s in spans if "make" in s.text)
        after = next(s for s in spans if "bold here" in s.text)
        assert before.font == "Helvetica"
        assert after.font == "Helvetica"
        # All three sit on the same baseline (one visual line).
        assert before.origin[1] == pytest.approx(bolded.origin[1], abs=0.3)
        assert after.origin[1] == pytest.approx(bolded.origin[1], abs=0.3)
    finally:
        window.close()


def test_superscript_single_character_in_body(qapp, tmp_path):
    """Super/subscript applied to ONE character inside a line of text."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        para = view.document.paragraph_at(0, 100, 112)
        view._begin_paragraph_edit(0, para)

        _select(view._para_editor, "words")
        window._super_action.setChecked(True)
        view._para_editor.commit()

        spans = view.document.text_spans(0)
        sup = next(s for s in spans if s.text.strip() == "words")
        body = next(s for s in spans if "make" in s.text)
        assert sup.size == pytest.approx(8.0 * 0.58, abs=0.2)  # scaled
        assert sup.origin[1] == pytest.approx(body.origin[1] - 8.0 * 0.35, abs=0.4)  # raised
    finally:
        window.close()


def test_selection_bold_in_single_line_editor(qapp, quote_pdf):
    """The span editor is rich too: bold part of a value."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        view._begin_text_edit(0, span)

        _select(view._editor, "234")
        window._bold_action.setChecked(True)
        view._editor.commit()

        spans = view.document.text_spans(0)
        bolded = next(s for s in spans if s.text.strip() == "234")
        assert bolded.font == "Helvetica-Bold"
        assert any(s.text.strip() == "$1," and s.font == "Helvetica" for s in spans)
    finally:
        window.close()


def test_both_editors_have_resize_grips(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        assert view._editor._grip is not None  # single-line box adjustable too
        assert view._para_editor._grip is not None
    finally:
        window.close()


def test_insert_uses_toolbar_style(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        window._size_spin.setValue(13.0)
        view._pending_insert = (0, (250.0, 500.0))
        view._on_paragraph_committed("Toolbar sized note")
        span = _find_span(view, "Toolbar sized note")
        assert span.size == pytest.approx(13.0, abs=0.1)
    finally:
        window.close()
