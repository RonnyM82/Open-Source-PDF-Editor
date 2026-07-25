"""Offscreen tests for click-to-edit: hit-test, overlay, engine commit, undo.

The commit path is driven directly (per the plan: no synthetic double-clicks);
the double-click -> pointActivated mapping itself is thin Qt glue over the
pure page_coords module, which has its own tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_coords import page_to_scene  # noqa: E402
from pdfapp.page_geometry import hover_target  # noqa: E402


def _page_text(view) -> str:
    return view.document._doc[0].get_text()


def _find_span(view, text):
    return next(s for s in view.document.text_spans(0) if s.text.strip() == text)


def _scene_center_of(view, span):
    cx = (span.bbox[0] + span.bbox[2]) / 2
    cy = (span.bbox[1] + span.bbox[3]) / 2
    return page_to_scene(
        cx,
        cy,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(0),
        page_size_pts=view.document.page_size(0),
    )


def test_point_on_span_opens_prefilled_editor(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        span = _find_span(view, quote_pdf.price)
        view._on_point_activated(*_scene_center_of(view, span))
        assert not view._editor.isHidden()
        assert view._editor.text() == span.text
    finally:
        window.close()


def test_miss_click_opens_nothing(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        # (10, 10) pts is inside the page but on no span (the logo starts at x=40).
        z = view._canvas.render_zoom
        view._on_point_activated(10 * z, 10 * z)
        assert view._editor.isHidden()
    finally:
        window.close()


def test_hit_test_works_on_rotated_page(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        window.rotate_clockwise()
        assert view.document.page_rotation(0) == 90
        span = _find_span(view, quote_pdf.price)  # bbox stays in unrotated space
        view._on_point_activated(*_scene_center_of(view, span))
        assert not view._editor.isHidden()
        assert view._editor.text() == span.text
    finally:
        window.close()


def test_commit_replaces_text_and_undo_restores(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        span = _find_span(view, quote_pdf.price)

        view._pending_edit = (0, span)
        view._on_edit_committed("$9,999.99")

        text = _page_text(view)
        assert "$9,999.99" in text
        assert quote_pdf.price not in text
        assert view.dirty

        view.undo_stack.undo()
        text = _page_text(view)
        assert quote_pdf.price in text
        assert "$9,999.99" not in text
        assert not view.dirty

        view.undo_stack.redo()
        assert "$9,999.99" in _page_text(view)
    finally:
        window.close()


def test_commit_same_text_pushes_no_command(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        span = _find_span(view, quote_pdf.price)
        view._begin_text_edit(0, span)  # captures the open style
        view._editor.commit()  # unchanged text AND unchanged style -> no-op
        assert view.undo_stack.count() == 0
        assert not view.dirty
    finally:
        window.close()


def test_edit_evicts_and_rerenders_page_cache(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        zoom = view._canvas.render_zoom
        before = view._cache.get((0, "main", zoom))
        assert before is not None

        span = _find_span(view, quote_pdf.price)
        view._pending_edit = (0, span)
        view._on_edit_committed("$4.20")

        after = view._cache.get((0, "main", zoom))
        assert after is not None
        assert after is not before  # stale render evicted, page re-rendered
    finally:
        window.close()


def test_editor_return_commits_and_escape_cancels(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        span = _find_span(view, quote_pdf.price)

        # Enter commits through the whole pipeline.
        view._begin_text_edit(0, span)
        view._editor.setText("$5.55")
        view._editor.returnPressed.emit()
        assert view._editor.isHidden()
        assert "$5.55" in _page_text(view)

        # Escape cancels without touching the document.
        new_span = _find_span(view, "$5.55")
        view._begin_text_edit(0, new_span)
        view._editor.setText("$6.66")
        escape = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        view._editor.keyPressEvent(escape)
        assert view._editor.isHidden()
        assert "$6.66" not in _page_text(view)
        assert view._pending_edit is None
    finally:
        window.close()


def test_focus_out_does_not_cancel_the_edit(qapp, quote_pdf):
    """Losing focus (e.g. clicking the style toolbar) must NOT discard the
    edit — that made restyling existing text impossible."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        span = _find_span(view, quote_pdf.price)
        view._begin_text_edit(0, span)
        assert view._editor.is_editing

        view._editor.clearFocus()  # as if a toolbar control took focus
        assert view._editor.is_editing  # still open
        assert not view._editor.isHidden()

        view._editor.setText("$7.77")
        view._editor.commit()  # committing after the focus change still works
        assert "$7.77" in _page_text(view)
    finally:
        window.close()


def test_background_click_commits_open_editor(qapp, quote_pdf):
    """Clicking the page background applies an in-progress edit (click-away)."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        span = _find_span(view, quote_pdf.price)
        view._begin_text_edit(0, span)
        view._editor.setText("$8.88")
        view._canvas.backgroundPressed.emit()  # click on the page background
        assert view._editor.isHidden()
        assert "$8.88" in _page_text(view)
    finally:
        window.close()


def test_paragraph_editor_appends_not_replaces(qapp, tmp_path):
    """The paragraph editor opens with the cursor at the END and nothing
    selected, so typing appends (the "it replaced instead of appended" bug)."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        para = view.document.paragraph_at(0, 100, 118)
        view._begin_paragraph_edit(0, para)

        cursor = view._para_editor.textCursor()
        assert not cursor.hasSelection()  # nothing selected -> a keystroke appends
        assert cursor.atEnd()

        view._para_editor.insertPlainText(" APPENDED")
        view._para_editor.commit()

        text = _page_text(view)
        # Appended, not replaced: the original lines survive AND the new word
        # is present (it may wrap to its own line inside the box — the point is
        # nothing was wiped).
        assert "APPENDED" in text
        assert "body first line of the paragraph" in text
        assert "body second line with more words" in text
        assert "body third and final line" in text
    finally:
        window.close()


def test_paragraph_editor_has_visible_corner_grip(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        from PySide6.QtCore import Qt

        para = view.document.paragraph_at(0, 100, 118)
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor
        grip = editor._grip
        # A visible, cursor-hinted child grip exists at the bottom-right.
        assert grip.parent() is editor
        assert grip.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
        editor.resize(200, 100)
        editor._reposition_grip()  # offscreen: resizeEvent isn't delivered sync
        assert grip.x() + grip.width() == 200
        assert grip.y() + grip.height() == 100
        # Marking user-sized (as the grip drag does) exposes the wrap width.
        editor.mark_user_sized()
        assert editor.user_sized_width == 200
    finally:
        window.close()


def test_editor_font_matches_span_on_screen(qapp, quote_pdf):
    """The overlay must not restyle the text: same family class, same
    on-screen size (fontsize x zoom), same weight — and light chrome."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args

        price = _find_span(view, quote_pdf.price)  # helv, 9pt, regular
        view._begin_text_edit(0, price)
        font = view._editor.font()
        assert font.family() == "Arial"
        assert font.pixelSize() == max(8, round(price.size * view._canvas.zoom))
        assert not font.bold()

        heading = _find_span(view, quote_pdf.heading)  # hebo, 16pt, bold
        view._begin_text_edit(0, heading)
        font = view._editor.font()
        assert font.family() == "Arial"
        assert font.bold()
        assert font.pixelSize() == max(8, round(heading.size * view._canvas.zoom))

        assert "background: white" in view._editor.styleSheet()
    finally:
        window.close()


# --- paragraph edit (E4.5) ----------------------------------------------


def _paragraph_pdf(tmp_path, mixed=False):
    """Header + tight 3-line body (one dict block), like the quote's cell."""
    import pymupdf

    path = tmp_path / "para.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "HEADER row", fontname="hebo", fontsize=8)
    first_font = "hebo" if mixed else "helv"
    page.insert_text((72, 111), "body first line of the paragraph", fontname=first_font, fontsize=8)
    page.insert_text((72, 119), "body second line with more words", fontsize=8)
    page.insert_text((72, 127), "body third and final line", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def test_ctrl_point_opens_paragraph_editor(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        z = view._canvas.render_zoom
        view._on_point_activated(100 * z, 118 * z, True)  # Ctrl+double-click path
        assert not view._para_editor.isHidden()
        lines = view._para_editor.toPlainText().splitlines()
        assert len(lines) == 3
        assert lines[0] == "body first line of the paragraph"
        assert "HEADER" not in view._para_editor.toPlainText()
        assert view._editor.isHidden()  # the span editor stays out of it
    finally:
        window.close()


def test_paragraph_commit_replaces_and_undo_restores(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        z = view._canvas.render_zoom
        view._on_point_activated(100 * z, 118 * z, True)
        para = view._pending_paragraph[1]

        view._pending_paragraph = (0, para)
        view._on_paragraph_committed("replacement first\nreplacement second")
        text = _page_text(view)
        assert "replacement first" in text and "replacement second" in text
        assert "body second" not in text
        assert "HEADER row" in text
        assert view.dirty

        view.undo_stack.undo()
        text = _page_text(view)
        assert "body second line with more words" in text
        assert "replacement first" not in text
        assert not view.dirty
    finally:
        window.close()


def test_paragraph_commit_same_text_pushes_nothing(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        para = view.document.paragraph_at(0, 100, 118)
        view._begin_paragraph_edit(0, para)  # captures the open style
        view._para_editor.commit()  # unchanged text/width/style -> no-op
        assert view.undo_stack.count() == 0
        assert not view.dirty
    finally:
        window.close()


def test_paragraph_mixed_styles_preserved_through_edit(qapp, tmp_path):
    """REWRITTEN for E9 (was test_paragraph_mixed_style_warns): mixed-style
    paragraphs used to flatten to the dominant style with a warning; the rich
    editor now PRESERVES per-span styles through an edit, so the bold first
    line must still be bold after an appending commit."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path, mixed=True))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        para = view.document.paragraph_at(0, 100, 118)
        assert not para.uniform_style  # first body line is hebo, rest helv

        view._begin_paragraph_edit(0, para)  # rich prefill from para.lines
        view._para_editor.insertPlainText(" TAIL")  # cursor at end -> appends
        view._para_editor.commit()

        spans = view.document.text_spans(0)
        bold_line = next(s for s in spans if "body first line" in s.text)
        assert bold_line.font == "Helvetica-Bold"  # style survived the edit
        plain_line = next(s for s in spans if "body second line" in s.text)
        assert plain_line.font == "Helvetica"
        assert "TAIL" in _page_text(view)
    finally:
        window.close()


def test_paragraph_growth_warns_and_extra_lines_land(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        messages: list[str] = []
        view.editWarning.connect(messages.append)
        para = view.document.paragraph_at(0, 100, 118)
        view._pending_paragraph = (0, para)
        view._on_paragraph_committed("\n".join(f"grown line {i}" for i in range(6)))

        assert any("grew to fit" in m for m in messages)
        assert "grown line 5" in _page_text(view)
        assert view.dirty
    finally:
        window.close()


def test_paragraph_impossible_fit_drops_command_and_reports(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        errors: list[str] = []
        view.editWarning.connect(errors.append)  # offscreen: non-modal path
        para = view.document.paragraph_at(0, 100, 118)
        view._pending_paragraph = (0, para)
        view._on_paragraph_committed("\n".join(f"line {i}" for i in range(200)))

        assert any("does not fit" in m for m in errors)  # surfaced to the user
        assert view.undo_stack.count() == 0  # obsolete command dropped
        assert not view.dirty
        assert "body second line with more words" in _page_text(view)  # untouched
    finally:
        window.close()


def test_paragraph_editor_width_resize_rewraps_on_commit(qapp, tmp_path):
    import pymupdf

    path = tmp_path / "wide.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "a single very long line of text that fills space", fontsize=10)
    doc.save(str(path))
    doc.close()

    window = MainWindow()
    try:
        window.open_path(path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        para = view.document.paragraph_at(0, 100, 98)
        view._pending_paragraph = (0, para)
        # Simulate a right-edge drag to ~120pt at the current zoom.
        target_px = int(120.0 * view._canvas.zoom) + 8
        view._para_editor.resize(target_px, view._para_editor.height())
        view._para_editor._user_sized = True
        view._on_paragraph_committed(para.text)  # same text, new width

        assert view.undo_stack.count() == 1  # width-only change is a real edit
        assert len([s for s in view.document.text_spans(0) if s.text.strip()]) >= 2
    finally:
        window.close()


def test_paragraph_editor_grows_while_typing(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        # Offscreen widgets get no real layout pass — give the canvas an
        # explicit size so the growth clamp has room to allow expansion.
        view._canvas.resize(800, 600)
        z = view._canvas.render_zoom
        view._on_point_activated(100 * z, 118 * z, True)
        editor = view._para_editor
        height_before = editor.height()
        editor.setPlainText(editor.toPlainText() + "\nextra one\nextra two\nextra three")
        assert editor.height() > height_before  # grew to keep new lines visible
    finally:
        window.close()


def _narrow_totals_pdf(tmp_path):
    """A narrow numbers column, like a quote's totals cell (tight 8pt pitch)."""
    import pymupdf

    path = tmp_path / "totals.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((500, 100), "1,210.47", fontsize=8)
    page.insert_text((500, 108), "200.00", fontsize=8)
    page.insert_text((500, 116), "$1,410.47", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def test_paragraph_editor_fits_narrow_block_without_wrapping(qapp, tmp_path):
    """User report: editing a narrow totals column wrapped '$1,410.47' onto a
    second visual line — the paragraph bbox leaves no room for the editor's
    chrome and the substituted font's wider metrics. The editor must open
    wide enough to show every logical line unwrapped."""
    window = MainWindow()
    try:
        window.open_path(_narrow_totals_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        view._canvas.resize(800, 600)
        z = view._canvas.render_zoom
        view._on_point_activated(510 * z, 107 * z, True)  # Ctrl+dblclick the column
        editor = view._para_editor
        assert not editor.isHidden()
        doc = editor.document()
        assert doc.blockCount() == 3
        assert editor.width() >= int(editor._content_width_px())  # room to render
        # Hidden widgets get no layout pass — lay the document out at the
        # editor's usable width, exactly what showing the widget would do.
        doc.setTextWidth(editor.width() - 2 * editor.frameWidth())
        doc.documentLayout().documentSize()
        visual_lines = sum(
            doc.findBlockByNumber(i).layout().lineCount() for i in range(doc.blockCount())
        )
        assert visual_lines == 3  # every value on its own line, no mid-number wrap
    finally:
        window.close()


def test_paragraph_editor_line_height_matches_pitch(qapp, tmp_path):
    """The editor's line spacing is pinned to the paragraph's pitch (scaled by
    the font's effective px-per-pt) — QTextEdit's looser natural spacing made
    tight blocks render visibly taller in the editor than on the page."""
    window = MainWindow()
    try:
        window.open_path(_narrow_totals_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        view._canvas.resize(800, 600)
        z = view._canvas.render_zoom
        view._on_point_activated(510 * z, 107 * z, True)
        editor = view._para_editor
        para = view._pending_paragraph[1]
        expected = para.pitch * editor.font().pixelSize() / para.size
        doc = editor.document()
        for i in range(doc.blockCount()):
            fmt = doc.findBlockByNumber(i).blockFormat()
            assert fmt.lineHeightType() == 2  # QTextBlockFormat FixedHeight
            assert fmt.lineHeight() == pytest.approx(expected, abs=0.5)
    finally:
        window.close()


def _right_aligned_totals_pdf(tmp_path):
    """The label pair of a right-aligned totals block (one MuPDF block)."""
    import pymupdf

    path = tmp_path / "right_totals.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(["Subtotal ex GST", "Shipping Cost (DHL Intl - 000000000)"]):
        w = pymupdf.get_text_length(line, fontname="helv", fontsize=10)
        page.insert_text((300 - w, 100 + i * 12), line, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def test_paragraph_editor_shows_right_alignment_and_anchors_right_edge(qapp, tmp_path):
    """User report: the editor showed right-aligned blocks left-justified.
    The editor blocks must carry the paragraph's alignment, and content-fit
    widening must grow LEFTWARD so the right edge stays over the page text."""
    window = MainWindow()
    try:
        window.open_path(_right_aligned_totals_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        view._canvas.resize(800, 600)
        span = next(s for s in view.document.text_spans(0) if "Subtotal" in s.text)
        z = view._canvas.render_zoom
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        view._on_point_activated(cx * z, cy * z, True)
        editor = view._para_editor
        assert not editor.isHidden()
        para = view._pending_paragraph[1]
        assert para.align == "right"
        doc = editor.document()
        for i in range(doc.blockCount()):
            fmt = doc.findBlockByNumber(i).blockFormat()
            assert fmt.alignment() & Qt.AlignmentFlag.AlignRight

        # Right edge of the editor tracks the paragraph's right edge (+2 rect
        # padding), however much the content fit widened the box leftward.
        from PySide6.QtCore import QPointF

        from pdfapp import page_coords

        scene = page_coords.page_rect_to_scene(
            para.bbox, render_zoom=z, rotation=0, page_size_pts=view.document.page_size(0)
        )
        expected_right = view._canvas.mapFromScene(QPointF(scene[2], scene[1])).x() + 2
        # The box grows leftward to keep its right edge over the page text,
        # stopping at the viewport's left edge (x=0). At the tiny offscreen
        # zoom the legibility-floored font needs more width than fits left of
        # the paragraph, so the invariant is: x is as far left as possible.
        assert editor.x() == max(0, expected_right - editor.width())
    finally:
        window.close()


def _label_pdf(tmp_path):
    import pymupdf

    path = tmp_path / "label.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), "Existing label value here", fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def test_inserted_box_not_dragged_when_moving_a_neighbour(qapp, tmp_path):
    """User bug: inserting a box one line below existing text and then moving
    the EXISTING line dragged the inserted box along (they merged). The box
    registry (stored in the document, E10) must keep them separate."""
    window = MainWindow()
    try:
        window.open_path(_label_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view._canvas.resize(800, 600)
        z = view._canvas.render_zoom

        # Insert a same-style box one line below, through the real insert path.
        view._pending_insert = (0, (100.0, 212.0))
        view._on_paragraph_committed("+64 21 555 0000")
        assert len(view.document.boxes(0)) == 1  # registered in the document
        phone_before = next(s for s in view.document.text_spans(0) if "+64" in s.text)

        # Grab the EXISTING line to move it — must resolve to it ALONE.
        target = hover_target(view.page_geometry(0), 150.0, 199.5)
        assert target is not None
        assert [s.text for s in target.payload.spans] == ["Existing label value here"]

        # Move it; the inserted phone box must stay put.
        view._on_move_drag_started(150.0 * z, 199.5 * z)
        assert view._move_paragraph is not None
        view._on_move_drag_finished(150.0 * z, 199.5 * z, 150.0 * z, 260.0 * z)

        phone_after = next(s for s in view.document.text_spans(0) if "+64" in s.text)
        assert phone_after.origin[1] == pytest.approx(phone_before.origin[1], abs=1.0)
        assert not any(
            "Existing label" in s.text and abs(s.origin[1] - 200.0) < 5
            for s in view.document.text_spans(0)
        )  # the existing line actually moved away
    finally:
        window.close()


def test_insert_undo_redo_keeps_registry_atomic(qapp, tmp_path):
    """Registration happens INSIDE the insert command, so undo removes the
    box's content AND its registry entry together; redo brings both back.
    (The old session tracking evaporated on undo — the re-merge bug.)"""
    window = MainWindow()
    try:
        window.open_path(_label_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view._pending_insert = (0, (100.0, 212.0))
        view._on_paragraph_committed("+64 21 555 0000")
        assert len(view.document.boxes(0)) == 1

        view.undo_stack.undo()
        assert view.document.boxes(0) == []  # content and identity both gone
        assert not any("+64" in s.text for s in view.document.text_spans(0))

        view.undo_stack.redo()
        assert len(view.document.boxes(0)) == 1  # both back, still in step
        assert any("+64" in s.text for s in view.document.text_spans(0))
    finally:
        window.close()


def test_moving_a_box_moves_its_registry_rect(qapp, tmp_path):
    """The registry rect follows the box, so isolation holds at the NEW spot
    (and across save/reopen — the registry lives in the document)."""
    window = MainWindow()
    try:
        window.open_path(_label_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view._canvas.resize(800, 600)
        z = view._canvas.render_zoom
        view._pending_insert = (0, (300.0, 400.0))
        view._on_paragraph_committed("+64 21 555 0000")
        before = view.document.boxes(0)[0]

        # Ctrl+drag the box itself 100pt down.
        cx = (before.rect[0] + before.rect[2]) / 2
        cy = (before.rect[1] + before.rect[3]) / 2
        view._on_move_drag_started(cx * z, cy * z)
        assert view._move_paragraph is not None
        view._on_move_drag_finished(cx * z, cy * z, cx * z, (cy + 100.0) * z)

        after = view.document.boxes(0)[0]
        assert after.id == before.id  # same identity...
        moved_cy = (after.rect[1] + after.rect[3]) / 2
        assert moved_cy == pytest.approx(cy + 100.0, abs=3.0)  # ...at the new spot
        # And the moved text is still isolated: grabbing it returns it alone.
        target = hover_target(view.page_geometry(0), cx, cy + 100.0)
        assert target is not None
        assert [s.text for s in target.payload.spans] == ["+64 21 555 0000"]
    finally:
        window.close()


def _two_boxes_view(window, tmp_path):
    """A view with two separately inserted text boxes (registered)."""
    import pymupdf

    path = tmp_path / "two_boxes.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()
    window.open_path(path)
    view = window.active_view
    view.set_edit_mode(True)
    view._canvas.resize(800, 600)
    for point, text in (((100.0, 200.0), "alpha box"), ((100.0, 300.0), "beta box")):
        view._pending_insert = (0, point)
        view._on_paragraph_committed(text)
    return view


def _para_containing(view, needle):
    # Through page_geometry — the real UI surface, which supplies the
    # registry boundaries (raw document.paragraphs(0) never sees them).
    return next(p for p in view.page_geometry(0).paragraphs if needle in p.text)


def test_ctrl_click_toggles_multi_selection(qapp, tmp_path):
    """E10.7: a Ctrl+CLICK (press+release without drag) toggles the box in
    the multi-selection; a second click on the same box removes it."""
    window = MainWindow()
    try:
        view = _two_boxes_view(window, tmp_path)
        z = view._canvas.render_zoom
        alpha = _para_containing(view, "alpha")
        cx = (alpha.bbox[0] + alpha.bbox[2]) / 2
        cy = (alpha.bbox[1] + alpha.bbox[3]) / 2

        view._on_move_drag_started(cx * z, cy * z)  # Ctrl+press...
        view._on_move_drag_finished(cx * z, cy * z, cx * z, cy * z)  # ...release
        assert len(view._multi_paragraphs) == 1

        view._on_move_drag_started(cx * z, cy * z)  # same box again: toggle off
        view._on_move_drag_finished(cx * z, cy * z, cx * z, cy * z)
        assert view._multi_paragraphs == []
    finally:
        window.close()


def test_group_move_keeps_relative_positions_one_undo(qapp, tmp_path):
    """E10.7: dragging a member of the multi-selection moves the WHOLE group
    by the same offset — relative positions preserved, ONE undo step."""
    window = MainWindow()
    try:
        view = _two_boxes_view(window, tmp_path)
        z = view._canvas.render_zoom
        alpha = _para_containing(view, "alpha")
        beta = _para_containing(view, "beta")
        gap_before = beta.bbox[1] - alpha.bbox[1]
        commands_before = view.undo_stack.count()

        view.toggle_multi_select(0, alpha)
        view.toggle_multi_select(0, beta)
        cx = (alpha.bbox[0] + alpha.bbox[2]) / 2
        cy = (alpha.bbox[1] + alpha.bbox[3]) / 2
        view._on_move_drag_started(cx * z, cy * z)
        assert view._move_group is not None  # routed as a GROUP move
        view._on_move_drag_finished(cx * z, cy * z, (cx + 50.0) * z, (cy + 40.0) * z)

        moved_alpha = _para_containing(view, "alpha")
        moved_beta = _para_containing(view, "beta")
        assert moved_alpha.bbox[0] == pytest.approx(alpha.bbox[0] + 50.0, abs=2.0)
        assert moved_alpha.bbox[1] == pytest.approx(alpha.bbox[1] + 40.0, abs=2.0)
        gap_after = moved_beta.bbox[1] - moved_alpha.bbox[1]
        assert gap_after == pytest.approx(gap_before, abs=1.0)  # relative kept
        assert view.undo_stack.count() == commands_before + 1  # ONE step

        view.undo_stack.undo()  # both come back together
        restored = _para_containing(view, "alpha")
        assert restored.bbox[1] == pytest.approx(alpha.bbox[1], abs=1.0)
    finally:
        window.close()


def test_merge_two_boxes_into_one(qapp, tmp_path):
    """E10.7: merging multi-selected boxes rebuilds them as ONE paragraph;
    two registered boxes collapse to one registry entry."""
    window = MainWindow()
    try:
        view = _two_boxes_view(window, tmp_path)
        assert len(view.document.boxes(0)) == 2
        alpha = _para_containing(view, "alpha")
        beta = _para_containing(view, "beta")
        view.toggle_multi_select(0, alpha)
        view.toggle_multi_select(0, beta)

        view._merge_selected_paragraphs()

        merged = [p for p in view.page_geometry(0).paragraphs if "alpha" in p.text]
        assert len(merged) == 1
        assert "beta box" in merged[0].text  # both lines in ONE paragraph
        assert len(view.document.boxes(0)) == 1  # registry collapsed too
        assert view._multi_paragraphs == []  # selection cleared (stale payloads)

        view.undo_stack.undo()
        assert len(view.document.boxes(0)) == 2  # atomic restore
    finally:
        window.close()


def test_merge_needs_at_least_two(qapp, tmp_path):
    window = MainWindow()
    try:
        view = _two_boxes_view(window, tmp_path)
        messages: list[str] = []
        view.editWarning.connect(messages.append)
        view.toggle_multi_select(0, _para_containing(view, "alpha"))
        view._merge_selected_paragraphs()
        assert any("two or more" in m for m in messages)
    finally:
        window.close()


# --- move paragraph (E5.1): Ctrl+drag glue ----------------------------------


def test_move_drag_moves_paragraph_and_undo_restores(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        z = view._canvas.render_zoom

        view._on_move_drag_started(100 * z, 118 * z)
        assert view._move_paragraph is not None
        before = next(s for s in view.document.text_spans(0) if "body first" in s.text)

        # Drag 50pt right, 30pt down (scene px = pts x render_zoom at rot 0).
        view._on_move_drag_finished(100 * z, 118 * z, 150 * z, 148 * z)
        after = next(s for s in view.document.text_spans(0) if "body first" in s.text)
        assert after.origin[0] == pytest.approx(before.origin[0] + 50.0, abs=0.75)
        assert after.origin[1] == pytest.approx(before.origin[1] + 30.0, abs=0.75)
        assert view.dirty

        view.undo_stack.undo()
        restored = next(s for s in view.document.text_spans(0) if "body first" in s.text)
        assert restored.origin[0] == pytest.approx(before.origin[0], abs=0.5)
        assert restored.origin[1] == pytest.approx(before.origin[1], abs=0.5)
        assert not view.dirty
    finally:
        window.close()


def test_move_drag_tiny_delta_is_a_noop(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        z = view._canvas.render_zoom
        view._on_move_drag_started(100 * z, 118 * z)
        # 0.4 page-pt of drift (the threshold is 1 pt in PAGE space).
        view._on_move_drag_finished(100 * z, 118 * z, 100.4 * z, 118.4 * z)
        assert view.undo_stack.count() == 0  # a click, not a move
        assert not view.dirty
    finally:
        window.close()


def test_move_drag_on_empty_area_ignored(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        z = view._canvas.render_zoom
        view._on_move_drag_started(400 * z, 400 * z)  # nothing there
        assert view._move_paragraph is None
        view._on_move_drag_finished(400 * z, 400 * z, 500 * z, 500 * z)
        assert view.undo_stack.count() == 0
    finally:
        window.close()


# --- insert new text (E5) --------------------------------------------------


def test_insert_text_click_to_place_and_undo(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args

        window.insert_text()  # menu action arms the canvas
        assert view._canvas._insert_armed

        z = view._canvas.render_zoom
        view._canvas.insertPointSelected.emit(250 * z, 500 * z)
        assert not view._para_editor.isHidden()
        assert view._para_editor.toPlainText() == ""

        view._para_editor.setPlainText("A brand new note")
        view._para_editor._commit()
        text = _page_text(view)
        assert "A brand new note" in text
        assert view.dirty

        view.undo_stack.undo()
        assert "A brand new note" not in _page_text(view)
        assert not view.dirty
    finally:
        window.close()


def test_insert_commits_at_the_toolbar_size(qapp, quote_pdf):
    """User bug: the insert editor showed the right size but the COMMITTED
    text came out at a different one (9pt at zoom 2 landed as 18pt) — the
    editor font is pixel-sized and the commit divided by a STALE zoom. The
    typing format now carries the true point size, so the committed span
    matches the toolbar exactly at any zoom."""
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        assert window._size_spin.value() == pytest.approx(9.0)  # launch default

        window.insert_text()
        z = view._canvas.render_zoom
        view._canvas.insertPointSelected.emit(250 * z, 500 * z)
        # Type through the cursor so the seeded typing format is inherited
        # (setPlainText would reset formats).
        view._para_editor.textCursor().insertText("SIZED TEXT")
        view._para_editor._commit()

        span = next(s for s in view.document.text_spans(0) if s.text == "SIZED TEXT")
        assert span.size == pytest.approx(9.0, abs=0.2)
    finally:
        window.close()


def test_size_spin_is_typeable_and_returns_focus(qapp, quote_pdf):
    """The size field accepts typed values (ClickFocus — the other toolbar
    controls stay NoFocus), applies once per entry (no per-keystroke apply),
    and hands focus back to the open editor when done."""
    window = MainWindow()
    try:
        from PySide6.QtCore import Qt

        assert window._size_spin.focusPolicy() == Qt.FocusPolicy.ClickFocus
        assert not window._size_spin.keyboardTracking()  # "12" isn't 1 then 12

        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        span = next(s for s in view.document.text_spans(0) if s.text.strip() == quote_pdf.price)
        view._begin_text_edit(0, span)
        assert view._editor.is_editing

        window._size_spin.setValue(12.0)  # what typing 12<Enter> emits
        window._refocus_open_editor()  # what editingFinished triggers
        assert view._editor.is_editing  # the editor stayed open throughout
    finally:
        window.close()


def test_insert_text_empty_commit_is_a_noop(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        view._pending_insert = (0, (250.0, 500.0))
        view._on_paragraph_committed("   ")
        assert view.undo_stack.count() == 0
        assert not view.dirty
    finally:
        window.close()


def test_overflow_warning_emitted(qapp, quote_pdf):
    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        messages: list[str] = []
        view.editWarning.connect(messages.append)

        span = _find_span(view, quote_pdf.price)
        view._pending_edit = (0, span)
        view._on_edit_committed("$1,234,567,890.00 plus all surcharges")
        assert any("wider" in m for m in messages)
    finally:
        window.close()


def test_embedded_font_warning_emitted_without_style_toolbar(qapp, embedded_font_pdf):
    """The "can't match exactly" warning belongs to AUTOMATIC matching — with
    a style toolbar the user's explicit choice is honoured instead, so this
    exercises the provider-less (engine-matching) path."""
    window = MainWindow()
    try:
        window.open_path(embedded_font_pdf)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)  # these tests drive the explicit line/paragraph args
        view.style_provider = None  # no toolbar: automatic matching
        messages: list[str] = []
        view.editWarning.connect(messages.append)

        span = next(s for s in view.document.text_spans(0) if "Embedded" in s.text)
        view._pending_edit = (0, span)
        view._on_edit_committed("Replaced")
        assert any("matched exactly" in m for m in messages)
        assert "Replaced" in _page_text(view)
    finally:
        window.close()


# --- overlay chrome: the grip clears any scrollbar (CAD review) ---------------


def test_grip_sits_clear_of_a_visible_scrollbar(qapp, quote_pdf):
    from PySide6.QtCore import QRect

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        editor = window.active_view._para_editor
        editor.open_at(QRect(10, 10, 160, 400), "\n".join(f"line {i}" for i in range(40)))
        editor.resize(160, 60)  # force content far taller than the box
        editor.grab()  # offscreen: force a layout pass so scrollbar ranges update
        vbar = editor.verticalScrollBar()
        assert vbar.maximum() > 0  # a scrollbar is up (range-based, offscreen-safe)
        assert editor._grip.x() + editor._grip.width() <= editor.width() - vbar.sizeHint().width()
        editor.cancel()
    finally:
        window.close()


def test_grip_stays_flush_without_scrollbars(qapp, quote_pdf):
    from PySide6.QtCore import QRect

    window = MainWindow()
    try:
        window.open_path(quote_pdf.path)
        editor = window.active_view._editor  # single-line: scrollbars always off
        editor.open_at(QRect(10, 10, 200, 30), "value")
        assert editor._grip.x() == editor.width() - editor._grip.width()
        assert editor._grip.y() == editor.height() - editor._grip.height()
        editor.cancel()
    finally:
        window.close()


def test_paragraph_editor_opens_without_a_scrollbar(qapp, tmp_path):
    """The fit height comes from the REAL layout now — a small tight block
    must not open pre-scrolled (the scrollbar hid content and the grip)."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        z = view._canvas.render_zoom
        view._on_point_activated(100 * z, 118 * z)  # paragraph-first default
        editor = view._para_editor
        assert editor.is_editing
        assert editor.verticalScrollBar().maximum() == 0  # everything visible
        editor.cancel()
    finally:
        window.close()


def test_ui_embedded_font_reused_on_paragraph_edit(qapp, embedded_nonstandard_font_pdf):
    """The commit path (editor pieces -> _runs_from_pieces) reuses the document's
    OWN embedded font for a Word-export paragraph: the run carries an embed_name
    intent and the saved edit re-embeds that font (not helv)."""
    window = MainWindow()
    try:
        window.open_path(embedded_nonstandard_font_pdf)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        para = view.document.paragraph_at(0, 200, 97)  # the full-width embedded line
        assert para is not None and para.embedded

        view._begin_paragraph_edit(0, para)
        assert view._edit_embedded_fonts  # base-family -> embedded name, populated at open
        pieces = view._para_editor._pieces()
        runs, _resolved = view._runs_from_pieces(pieces)
        assert any(r.style.embed_name for r in runs)  # editor pieces map back to the doc font

        view._pending_paragraph = (0, para)
        view._on_paragraph_committed(para.text.replace("stretches", "reaches"))
        text = _page_text(view)
        assert "reaches" in text and "stretches" not in text
        fonts = {f[3] for f in view.document._doc[0].get_fonts()}
        assert any("Calibri" in n or "Verdana" in n for n in fonts)
    finally:
        window.close()


def _wide_paragraph_pdf(tmp_path):
    """A wide 3-line prose paragraph (one dict block) — the reflow case."""
    import pymupdf

    path = tmp_path / "wide.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for i, line in enumerate(
        [
            "This is a wide flowing paragraph that fills the whole text column across the page",
            "and continues onto a second line running the full column width before ending soon",
            "on a short third line.",
        ]
    ):
        page.insert_text((72, 100 + i * 14), line, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def test_ui_wide_paragraph_reflows_in_editor(qapp, tmp_path):
    """A wide wrapped paragraph opens as ONE reflowable editor block (not one
    frozen block per visual line), and an unchanged commit is still a no-op."""
    window = MainWindow()
    try:
        window.open_path(_wide_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        para = view.document.paragraph_at(0, 100, 97)
        assert para is not None and len(para.lines) == 3
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor
        assert editor.document().blockCount() == 1  # 3 wraps -> 1 reflowable block

        editor.commit()  # unchanged content
        assert view.undo_stack.count() == 0  # no command (reflow no-op preserved)
    finally:
        window.close()


def test_ui_font_change_commits_on_word_export(qapp, real_embedded_bug_pdf):
    """End-to-end: changing the font of a wide Word-export paragraph commits
    (reflow) instead of being refused with the growth error."""
    from PySide6.QtGui import QTextCharFormat, QTextCursor

    window = MainWindow()
    try:
        window.open_path(real_embedded_bug_pdf)
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        warnings: list[str] = []
        view.editWarning.connect(warnings.append)
        para = next(
            p
            for p in view.document.paragraphs(0)
            if len(p.lines) >= 2 and (p.bbox[2] - p.bbox[0]) >= 200
        )
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor
        cursor = editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        editor.setTextCursor(cursor)
        fmt = QTextCharFormat()
        fmt.setFontFamilies(["Arial"])
        assert view.apply_format_to_editor(fmt)  # merged into the whole selection

        editor.commit()  # captures the rich (Arial) pieces and drives the real path
        assert view.undo_stack.count() == 1  # committed, not refused
        assert not any("failed" in w.lower() for w in warnings)
    finally:
        window.close()


def _bullet_list_pdf(tmp_path):
    """A bulleted list: a bullet at x=90 sharing the body's baseline (x=108)."""
    import pymupdf

    path = tmp_path / "bullets.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((90, 100), "•", fontsize=11)
    page.insert_text(
        (108, 100), "First item body running fairly wide across the column now", fontsize=11
    )
    page.insert_text((108, 116), "wrapping onto a second line here.", fontsize=11)
    page.insert_text((90, 150), "•", fontsize=11)
    page.insert_text((108, 150), "Second short item.", fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def test_ui_bullet_item_opens_grouped_with_marker(qapp, tmp_path):
    """Double-clicking a bulleted item opens the WHOLE item (marker + body) as
    one hanging-indent paragraph, and an unchanged commit is a no-op."""
    window = MainWindow()
    try:
        window.open_path(_bullet_list_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        para = view.document.paragraph_at(0, 150, 148)  # the second item
        assert para is not None and para.hang_indent > 0
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor
        assert editor.toPlainText().lstrip()[:1] in ("•", "·")  # marker shown
        editor.commit()  # unchanged
        assert view.undo_stack.count() == 0
    finally:
        window.close()


def test_ui_edit_bullet_item_keeps_marker_and_hang(qapp, tmp_path):
    """Editing a bulleted item's body commits, preserves the marker glyph and
    the hanging indent, and the item stays grouped."""
    from PySide6.QtGui import QTextCursor

    window = MainWindow()
    try:
        window.open_path(_bullet_list_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        para = view.document.paragraph_at(0, 150, 148)
        view._begin_paragraph_edit(0, para)
        editor = view._para_editor
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
        editor.insertPlainText(" APPENDED")
        editor.commit()
        assert view.undo_stack.count() == 1  # committed, not refused

        items = [
            p for p in view.document.paragraphs(0) if p.hang_indent > 0 and "APPENDED" in p.text
        ]
        assert items, "edited item lost its grouping/hang"
        assert items[0].text.lstrip()[:1] in ("•", "·")  # marker survived
    finally:
        window.close()


def test_ui_format_paragraph_as_list_and_clear(qapp, tmp_path):
    """The context-menu 'Format as list' converts a paragraph to a bulleted
    item and back, each as one undoable command."""
    window = MainWindow()
    try:
        window.open_path(_wide_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        para = view.document.paragraph_at(0, 200, 97)
        assert para is not None and para.hang_indent == 0.0

        view._apply_list_style("bullet", 0, [para])
        assert view.undo_stack.count() == 1
        items = [p for p in view.document.paragraphs(0) if p.hang_indent > 0]
        assert items and items[0].text.lstrip()[:1] in ("•", "·")

        view._apply_list_style(None, 0, [items[0]])  # unlist
        assert view.undo_stack.count() == 2
        assert not any(p.hang_indent > 0 for p in view.document.paragraphs(0))

        view.undo_stack.undo()  # back to bulleted
        assert any(p.hang_indent > 0 for p in view.document.paragraphs(0))
    finally:
        window.close()


def test_ui_indent_list_item(qapp, tmp_path):
    """Increase-indent shifts a list item right as one undoable command."""
    window = MainWindow()
    try:
        window.open_path(_wide_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        para = view.document.paragraph_at(0, 200, 97)
        view._apply_list_style("bullet", 0, [para])
        item = next(p for p in view.document.paragraphs(0) if p.hang_indent > 0)
        left0 = item.bbox[0]
        view._indent_list_items(18.0, 0, [item])
        after = next(p for p in view.document.paragraphs(0) if p.hang_indent > 0)
        assert after.bbox[0] > left0 + 10  # shifted right, still a bullet
    finally:
        window.close()


def test_ui_embedded_font_map_empty_for_non_embedded(qapp, tmp_path):
    """A non-embedded (base-14) paragraph populates no embedded-font map, so the
    commit path takes the normal helv/base-14 route."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view.set_dblclick_paragraph(False)
        para = view.document.paragraph_at(0, 100, 118)
        view._begin_paragraph_edit(0, para)
        assert view._edit_embedded_fonts == {}
    finally:
        window.close()
