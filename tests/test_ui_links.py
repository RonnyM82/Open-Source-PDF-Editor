"""Offscreen tests for the hyperlink UI: create, follow, edit, delete, move,
reveal chrome, hover, and mode gating."""

from __future__ import annotations

import pymupdf
import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QDesktopServices  # noqa: E402

from pdfapp import page_coords  # noqa: E402
from pdfapp.link_dialog import LinkDialog  # noqa: E402
from pdfapp.main_window import MainWindow  # noqa: E402
from pdfapp.page_geometry import HoverTarget, hover_target  # noqa: E402
from pdfcore import links  # noqa: E402


def _standalone_link_pdf(tmp_path, rect=(100, 300, 300, 340), uri="https://standalone.example"):
    """A page whose link sits over BLANK space (grabbable for move/resize)."""
    base = tmp_path / "sl_base.pdf"
    d = pymupdf.open()
    d.new_page(width=400, height=500)
    d.save(str(base))
    d.close()
    d = pymupdf.open(str(base))
    d[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(rect), "uri": uri})
    out = tmp_path / "sl.pdf"
    d.save(str(out))
    d.close()
    return out


def _scene(view, n, px, py):
    return page_coords.page_to_scene(
        px,
        py,
        render_zoom=view._canvas.render_zoom,
        rotation=view.document.page_rotation(n),
        page_size_pts=view.document.page_size(n),
    )


# --- read / reveal / hover ----------------------------------------------------


def test_read_existing_links_via_cache(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        infos = view.page_links(0)
        assert len(infos) == 2
        assert any(i.uri == links_pdf.uri for i in infos)
    finally:
        window.close()


def test_link_reveal_chrome_only_in_edit_mode(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        assert view._canvas._link_rects == []  # markup: no reveal
        view.set_edit_mode(True)
        assert len(view._canvas._link_rects) == 2  # both links revealed
        view.set_show_editable_areas(False)
        assert view._canvas._link_rects == []  # reveal off clears them
    finally:
        window.close()


def test_hover_over_link_shows_hand_cursor_markup(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        x0, y0, x1, y1 = links_pdf.uri_rect
        sx, sy = _scene(view, 0, (x0 + x1) / 2, (y0 + y1) / 2)
        view._canvas.hoverMoved.emit(sx, sy)
        assert view._canvas._link_hover is True
        # Off the link -> hand cursor cleared.
        ox, oy = _scene(view, 0, 5, 5)
        view._canvas.hoverMoved.emit(ox, oy)
        assert view._canvas._link_hover is False
    finally:
        window.close()


def test_hover_target_link_is_lowest_priority(qapp, links_pdf, tmp_path):
    window = MainWindow()
    try:
        # A link OVER text resolves as text (text wins); a standalone one as link.
        window.open_path(links_pdf.path)
        view = window.active_view
        span = next(s for s in view.document.text_spans(0) if "Visit" in s.text)
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        geom = view.page_geometry(0)
        over_text = hover_target(geom, cx, cy)  # link rect covers this span
        assert over_text is not None and over_text.kind == "text"

        window.open_path(_standalone_link_pdf(tmp_path))
        view2 = window.active_view
        geom2 = view2.page_geometry(0)
        standalone = hover_target(geom2, 200, 320)
        assert standalone is not None and standalone.kind == "link_move"
    finally:
        window.close()


# --- create -------------------------------------------------------------------


def test_insert_link_arms_and_toggles(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        window.insert_link()
        assert view._canvas.link_rect_armed is True
        assert view.armed_action == "link"
        window.insert_link()  # clicking the checked action cancels
        assert view._canvas.link_rect_armed is False
    finally:
        window.close()


def test_insert_link_gated_to_edit_mode(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view  # markup mode by default
        view.begin_insert_link()
        assert view._canvas.link_rect_armed is False
    finally:
        window.close()


def test_commit_add_uri_link_and_undo(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        before = len(view.page_links(0))
        view._commit_add_link(0, (300, 400, 480, 430), {"uri": "https://new.example"})
        assert len(view.page_links(0)) == before + 1
        assert view.dirty
        view.undo_stack.undo()
        assert len(view.page_links(0)) == before
    finally:
        window.close()


def test_link_rect_too_small_warns(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        warnings = []
        view.editWarning.connect(warnings.append)
        s0 = _scene(view, 0, 100, 100)
        s1 = _scene(view, 0, 101, 101)
        view._on_link_rect_selected(s0[0], s0[1], s1[0], s1[1])
        assert warnings and "rectangle" in warnings[-1].lower()
        assert view.undo_stack.count() == 0
    finally:
        window.close()


# --- follow -------------------------------------------------------------------


def test_follow_goto_link_navigates(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        goto = next(i for i in view.page_links(0) if i.kind == links.GOTO)
        view._follow_link(goto)
        assert view.current_page == links_pdf.goto_page
    finally:
        window.close()


def test_follow_uri_link_opens_url(qapp, links_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
        uri = next(i for i in view.page_links(0) if i.kind == links.URI)
        view._follow_link(uri)
        assert opened == [links_pdf.uri]
    finally:
        window.close()


def test_markup_click_follows_link(qapp, links_pdf, monkeypatch):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view  # markup mode
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
        x0, y0, x1, y1 = links_pdf.uri_rect
        sx, sy = _scene(view, 0, (x0 + x1) / 2, (y0 + y1) / 2)
        view._on_select_drag_started(sx, sy)  # press on the link
        view._on_text_select_finished(sx, sy)  # release at same point = click
        assert opened == [links_pdf.uri]
    finally:
        window.close()


# --- edit / delete ------------------------------------------------------------


def test_commit_update_link_changes_target(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        uri = next(i for i in view.page_links(0) if i.kind == links.URI)
        view._commit_update_link(0, uri.xref, {"uri": "https://changed.example"})
        assert any(i.uri == "https://changed.example" for i in view.page_links(0))
    finally:
        window.close()


def test_delete_link_and_undo(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        view.set_edit_mode(True)
        uri = next(i for i in view.page_links(0) if i.kind == links.URI)
        view._delete_link_at(0, uri.xref)
        assert len(view.page_links(0)) == 1
        view.undo_stack.undo()
        assert len(view.page_links(0)) == 2
    finally:
        window.close()


def test_delete_selected_link_via_key(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_standalone_link_pdf(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        info = view.page_links(0)[0]
        view._selection = ("link", 0, info)
        view._on_delete_selection()
        assert view.page_links(0) == []
    finally:
        window.close()


# --- move / resize ------------------------------------------------------------


def test_move_standalone_link(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_standalone_link_pdf(tmp_path, rect=(100, 300, 300, 340)))
        view = window.active_view
        view.set_edit_mode(True)
        info = view.page_links(0)[0]
        target = HoverTarget("link_move", info.bbox, payload=info)
        view._accept_target_drag(0, 200, 320, target)
        assert view._move_link is not None
        s0 = _scene(view, 0, 200, 320)
        s1 = _scene(view, 0, 230, 360)  # +30, +40
        view._on_move_drag_finished(s0[0], s0[1], s1[0], s1[1])
        moved = view.page_links(0)[0]
        assert moved.bbox[0] == pytest.approx(130, abs=2)
        assert moved.bbox[1] == pytest.approx(340, abs=2)
    finally:
        window.close()


def test_resize_standalone_link(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_standalone_link_pdf(tmp_path, rect=(100, 300, 300, 340)))
        view = window.active_view
        view.set_edit_mode(True)
        info = view.page_links(0)[0]
        anchor = (100, 300)  # top-left fixed; drag the bottom-right corner
        view._finish_link_resize(0, info, anchor, *_scene(view, 0, 260, 380))
        r = view.page_links(0)[0].bbox
        assert r == pytest.approx((100, 300, 260, 380), abs=2)
    finally:
        window.close()


# --- dialog -------------------------------------------------------------------


def test_link_dialog_uri_spec(qapp):
    dlg = LinkDialog(None, page_count=3)
    dlg._uri_radio.setChecked(True)
    dlg._uri_edit.setText("  https://x.example  ")
    assert dlg.spec() == {"uri": "https://x.example"}
    assert dlg.removed is False


def test_link_dialog_page_spec(qapp):
    dlg = LinkDialog(None, page_count=5)
    dlg._page_radio.setChecked(True)
    dlg._page_spin.setValue(4)
    assert dlg.spec() == {"dest_page": 3}


def test_link_dialog_seeds_from_existing(qapp):
    info = links.LinkInfo(0, 7, links.GOTO, (10, 10, 100, 40), dest_page=2)
    dlg = LinkDialog(None, page_count=5, initial=info)
    assert dlg._page_radio.isChecked()
    assert dlg._page_spin.value() == 3


# --- action enablement --------------------------------------------------------


def test_insert_link_action_enabled_only_in_edit_mode(qapp, links_pdf):
    window = MainWindow()
    try:
        window.open_path(links_pdf.path)
        view = window.active_view
        assert not window._insert_link_action.isEnabled()  # markup
        view.set_edit_mode(True)
        assert window._insert_link_action.isEnabled()
    finally:
        window.close()


# --- Word-style text links (H2) -----------------------------------------------


def _paragraph_doc(tmp_path, text="Please click here now"):
    base = tmp_path / "ptext.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=400, height=300)
    pg.insert_text((60, 100), text, fontname="helv", fontsize=12)
    d.save(str(base))
    d.close()
    return base


def _sel_rect(view, wanted):
    ws = view.document._doc[0].get_text("words")  # noqa: SLF001 - test rig
    sel = [w for w in ws if w[4] in wanted]
    return (
        min(w[0] for w in sel),
        min(w[1] for w in sel),
        max(w[2] for w in sel),
        max(w[3] for w in sel),
    )


def test_link_text_tool_arms_and_toggles(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_doc(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        window.link_text()
        assert view.armed_action == "link_text"
        assert window._link_text_action.isChecked()
        window.link_text()
        assert view.armed_action is None
    finally:
        window.close()


def test_link_text_gated_to_edit_mode(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_doc(tmp_path))
        view = window.active_view  # markup
        view.begin_link_text()
        assert view.armed_action is None
    finally:
        window.close()


def test_commit_text_link_styles_and_links(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_doc(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        rect = _sel_rect(view, {"click", "here"})
        para = view.document.paragraph_at(0, (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        assert view._paragraph_style_editable(0, para, [rect])
        view._commit_text_link(0, [rect], para, {"uri": "https://ex.example"}, True)
        spans = view.document.text_spans(0)
        assert any(s.color == links.WORD_LINK_BLUE and s.underline for s in spans)
        assert view.document.links(0)[0].uri == "https://ex.example"
        # One undo step reverts BOTH the styling and the link.
        view.undo_stack.undo()
        assert not any(s.color == links.WORD_LINK_BLUE for s in view.document.text_spans(0))
        assert view.document.links(0) == []
    finally:
        window.close()


def test_commit_text_link_fallback_underline(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_doc(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        warnings = []
        view.editWarning.connect(warnings.append)
        rect = _sel_rect(view, {"click", "here"})
        # para=None forces the fallback (drawn underline, no recolour).
        view._commit_text_link(0, [rect], None, {"uri": "https://fb.example"}, True)
        assert view.document.links(0)[0].uri == "https://fb.example"
        drawings = view.document._doc[0].get_drawings()  # noqa: SLF001
        assert any(
            d.get("color") and abs(d["color"][2] - links.WORD_LINK_BLUE_RGB[2]) < 0.05
            for d in drawings
        )
        assert warnings and "underlined" in warnings[-1].lower()
    finally:
        window.close()


def test_redefine_link_area(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_doc(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        view._commit_add_link(0, (50, 200, 150, 220), {"uri": "https://re.example"})
        info = view.document.links(0)[0]
        view._redefine_link_area(0, info)
        assert view._canvas.link_rect_armed and view._pending_redefine == (0, info.xref)
        s0 = _scene(view, 0, 200, 240)
        s1 = _scene(view, 0, 320, 270)
        view._on_link_rect_selected(s0[0], s0[1], s1[0], s1[1])
        r = view.document.links(0)[0].bbox
        assert r[0] == pytest.approx(200, abs=3) and r[2] == pytest.approx(320, abs=3)
    finally:
        window.close()


def test_double_click_linked_text_opens_editor(qapp, tmp_path):
    """Linked text stays editable — double-click opens the text/paragraph
    editor (links are lowest priority), so its style can be changed."""
    window = MainWindow()
    try:
        window.open_path(_paragraph_doc(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        rect = _sel_rect(view, {"click", "here"})
        para = view.document.paragraph_at(0, (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        view._commit_text_link(0, [rect], para, {"uri": "https://ex.example"}, True)
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        sx, sy = _scene(view, 0, cx, cy)
        view._on_point_activated(sx, sy, False)
        assert view._editor.is_editing or view._para_editor.is_editing
    finally:
        window.close()


def test_link_dialog_style_checkbox(qapp):
    create = LinkDialog(None, page_count=3, text_link=True)
    assert create.style_as_hyperlink() is True  # default on
    box = LinkDialog(None, page_count=3, text_link=False)
    assert box.style_as_hyperlink() is False  # no checkbox for a drawn box
    info = links.LinkInfo(0, 7, links.URI, (10, 10, 100, 40), uri="https://x")
    editing = LinkDialog(None, page_count=3, initial=info, text_link=True)
    assert editing.style_as_hyperlink() is False  # not offered when editing


# --- auto-detect URLs (H3) ----------------------------------------------------


def _urls_doc(tmp_path, line="Visit https://example.com/quote and rep@example.com"):
    base = tmp_path / "urls.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=520, height=300)
    pg.insert_text((50, 90), line, fontname="helv", fontsize=11)
    d.save(str(base))
    d.close()
    return base


def test_detect_links_action_enabled_only_in_edit_mode(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_urls_doc(tmp_path))
        view = window.active_view
        window._sync_chrome()
        assert not window._detect_links_action.isEnabled()  # markup
        view.set_edit_mode(True)
        window._sync_chrome()
        assert window._detect_links_action.isEnabled()
    finally:
        window.close()


def test_detect_and_link_urls_creates_styled_links_and_undo(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_urls_doc(tmp_path))
        view = window.active_view
        view.set_edit_mode(True)
        window.detect_links()  # offscreen: skips the confirm, links directly
        got = {i.uri for i in view.document.links(0)}
        assert got == {"https://example.com/quote", "mailto:rep@example.com"}
        assert any(s.color == links.WORD_LINK_BLUE for s in view.document.text_spans(0))
        assert view.dirty
        view.undo_stack.undo()
        assert view.document.links(0) == []
    finally:
        window.close()


def test_detect_and_link_urls_none_reports(qapp, tmp_path):
    window = MainWindow()
    try:
        window.open_path(_paragraph_doc(tmp_path, "no addresses in this text at all"))
        view = window.active_view
        view.set_edit_mode(True)
        warnings = []
        view.editWarning.connect(warnings.append)
        view.detect_and_link_urls()
        assert view.document.links(0) == []
        assert warnings and "no web" in warnings[-1].lower()
    finally:
        window.close()
