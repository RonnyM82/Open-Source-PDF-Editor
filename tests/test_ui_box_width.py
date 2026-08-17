"""An inserted text box gets an honest wrap width (BW3).

A box used to register as wide as the glyphs first typed into it, and the
engine wrapped a re-edit to that ink, so adding one word manufactured a second
line and the E9.4 growth-collision check refused the edit outright: you could
not add a word to a box you created five minutes ago. These tests drive the
real commit handlers offscreen (the established pattern — dialogs and menus
gate on isVisible).
"""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from pdfapp.main_window import MainWindow  # noqa: E402


def _pdf(tmp_path, name="bw.pdf", below=True, right_of=None):
    """A page with room at (100, 117) for an inserted label, a paragraph one
    pitch below it (the E9.4 trap) and optionally an obstacle to its right."""
    path = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    if below:
        page.insert_text((100, 127), "International Bank or Wire Fees", fontname="helv", fontsize=9)
        page.insert_text((100, 140), "apply to overseas payments.", fontname="helv", fontsize=9)
    if right_of is not None:
        page.insert_text((right_of, 117), "COLUMN", fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _view(window, path):
    window.open_path(path)
    view = window.active_view
    view.set_edit_mode(True)
    view._canvas.resize(900, 700)
    return view


def _insert_box(view, text="Terms of trade", point=(100.0, 117.0), drag_width=None):
    """Insert a text box through the real insert editor; returns its BoxRecord."""
    px, py = point
    view._pending_insert = (0, point)
    view._open_insert_editor(0, px, py, *view._page_point_to_scene(px, py, 0))
    editor = view._para_editor
    editor.insertPlainText(text)
    if drag_width is not None:  # what dragging the editor's right edge does
        editor.resize(drag_width, editor.height())
        editor.mark_user_sized()
    editor.commit()
    return view.document.boxes(0)[-1]


def _para(view, needle):
    return next(p for p in view.page_geometry(0).paragraphs if needle in p.text)


def _edit_paragraph(view, needle, new_text, drag_width=None):
    """Open the paragraph editor on the box holding ``needle``, retype, commit."""
    view._begin_paragraph_edit(0, _para(view, needle))
    editor = view._para_editor
    editor.setPlainText(new_text)
    if drag_width is not None:
        editor.resize(drag_width, editor.height())
        editor.mark_user_sized()
    editor.commit()


def test_inserted_box_registers_with_no_chosen_width(qapp, tmp_path):
    """A plain insert chooses no width, so the room is measured per edit."""
    window = MainWindow()
    try:
        view = _view(window, _pdf(tmp_path))
        box = _insert_box(view)
        assert box.width == 0.0
        assert box.rect[2] - box.rect[0] < 70.0  # still ink-wide: that is fine
    finally:
        window.close()


def test_adding_words_to_an_inserted_box_is_not_refused(qapp, tmp_path):
    """The reported defect, end to end through the UI: a box one pitch above
    existing text takes two more words on ONE line instead of being refused."""
    window = MainWindow()
    try:
        view = _view(window, _pdf(tmp_path))
        warnings: list[str] = []
        view.editWarning.connect(warnings.append)
        _insert_box(view)

        _edit_paragraph(view, "Terms of trade", "Terms of trade apply here")
        assert not [w for w in warnings if "already" in w]  # the E9.4 refusal
        edited = _para(view, "Terms of trade")
        assert edited.text == "Terms of trade apply here"
        assert len(edited.lines) == 1  # one line, not wrapped to the old ink
        # The bystander paragraph below is untouched.
        assert _para(view, "International").text.startswith("International Bank")
    finally:
        window.close()


def test_a_hemmed_in_box_still_refuses(qapp, tmp_path):
    """E9.4 keeps its say: with an obstacle to the right and text below, there
    genuinely is no room, and the edit is refused rather than overprinted."""
    window = MainWindow()
    try:
        view = _view(window, _pdf(tmp_path, right_of=170.0))
        warnings: list[str] = []
        view.editWarning.connect(warnings.append)
        _insert_box(view)

        _edit_paragraph(view, "Terms of trade", "Terms of trade apply here as well")
        assert [w for w in warnings if "already" in w]
        assert _para(view, "Terms of trade").text == "Terms of trade"  # unchanged
    finally:
        window.close()


def test_pre_existing_paragraph_keeps_its_own_column_width(qapp, tmp_path):
    """Real documents must not re-wrap: a paragraph no registered box owns gets
    no width from this feature, so it still wraps inside its own box."""
    window = MainWindow()
    try:
        view = _view(window, _pdf(tmp_path))
        para = _para(view, "International")
        assert view._box_wrap_width(view.document, 0, para, None) is None

        _edit_paragraph(
            view,
            "International",
            "International Bank or Wire Fees apply to overseas payments.",
        )
        # Re-wrapped within its own ~150 pt box, not run out to the page margin.
        assert len(_para(view, "International").lines) >= 2
    finally:
        window.close()


def test_dragged_width_persists_and_is_reused(qapp, tmp_path):
    """A drag is the user choosing a width, so it is stored with the box and the
    NEXT edit starts from it (it used to be forgotten when the editor closed)."""
    window = MainWindow()
    try:
        view = _view(window, _pdf(tmp_path, below=False))
        _insert_box(view)
        zoom = view._canvas.zoom
        _edit_paragraph(view, "Terms of trade", "Terms of trade", drag_width=int(60 * zoom + 8))

        box = view.document.boxes(0)[0]
        assert box.width == pytest.approx(60.0, abs=1.0)
        assert view._box_wrap_width(view.document, 0, _para(view, "Terms"), box) == box.width

        # A later edit wraps at the width the user chose, not at the page margin.
        _edit_paragraph(view, "Terms of trade", "Terms of trade apply here")
        assert len(_para(view, "Terms of trade").lines) > 1
        assert view.document.boxes(0)[0].width == pytest.approx(60.0, abs=1.0)
    finally:
        window.close()


def test_editor_opens_at_the_width_the_commit_uses(qapp, tmp_path):
    """What the user watches wrapping is what wraps: the editor box opens at the
    box's wrap width (converted px <-> pt the same way the commit does)."""
    window = MainWindow()
    try:
        view = _view(window, _pdf(tmp_path, below=False))
        box = _insert_box(view)
        # A real zoom: an offscreen canvas never lays out, so it otherwise sits
        # at _MIN_ZOOM where the editor's 8 px font floor dominates every width.
        view._canvas._set_zoom(1.0)
        para = _para(view, "Terms of trade")
        wrap = view._box_wrap_width(view.document, 0, para, box)
        assert wrap > para.bbox[2] - para.bbox[0]  # wider than the ink it holds

        view._begin_paragraph_edit(0, para)
        expected = wrap * view._canvas.zoom + 8  # the commit's px <-> pt rule
        assert view._para_editor.width() == pytest.approx(expected, abs=12)
        view._para_editor.cancel()
    finally:
        window.close()
