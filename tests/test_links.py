"""Engine hyperlink ops: read, create, edit, move/resize, delete, guard.

Round-trip style (open -> operate -> save -> reopen -> assert), per CLAUDE.md
rule 10 — the link-mutation APIs' page-cache staleness makes an in-memory-only
assertion especially misleading.
"""

from __future__ import annotations

import pymupdf
import pytest

from pdfcore import links
from pdfcore.document import PdfDocument


def _links(path, page_index=0):
    doc = pymupdf.open(str(path))
    try:
        return links.links_on_page(doc, page_index)
    finally:
        doc.close()


# --- read / recognise ---------------------------------------------------------


def test_read_existing_links(links_pdf):
    with PdfDocument.open(links_pdf.path) as doc:
        infos = doc.links(0)
        assert len(infos) == 2
        uri = next(i for i in infos if i.kind == links.URI)
        goto = next(i for i in infos if i.kind == links.GOTO)
        assert uri.uri == links_pdf.uri
        assert uri.bbox == pytest.approx(links_pdf.uri_rect, abs=1.0)
        assert uri.editable
        assert goto.dest_page == links_pdf.goto_page
        assert goto.bbox == pytest.approx(links_pdf.goto_rect, abs=1.0)
        # Other pages carry no links.
        assert doc.links(1) == []


def test_link_at_hit_and_miss(links_pdf):
    with PdfDocument.open(links_pdf.path) as doc:
        x0, y0, x1, y1 = links_pdf.uri_rect
        hit = doc.link_at(0, (x0 + x1) / 2, (y0 + y1) / 2)
        assert hit is not None and hit.uri == links_pdf.uri
        assert doc.link_at(0, 5.0, 5.0) is None


def test_link_at_smallest_wins(text_pdf, tmp_path):
    out = tmp_path / "nested.pdf"
    with PdfDocument.open(text_pdf) as doc:
        doc.add_link(0, (50, 50, 250, 250), uri="https://outer.example")
        doc.add_link(0, (100, 100, 150, 150), uri="https://inner.example")
        doc.save(out)
    doc = pymupdf.open(str(out))
    try:
        hit = links.link_at(doc, 0, 120, 120)
        assert hit.uri == "https://inner.example"
    finally:
        doc.close()


# --- create -------------------------------------------------------------------


def test_add_uri_link_roundtrip(text_pdf, tmp_path):
    out = tmp_path / "uri.pdf"
    with PdfDocument.open(text_pdf) as doc:
        xref = doc.add_link(0, (72, 200, 300, 220), uri="mailto:sales@example.com")
        assert xref > 0
        doc.save(out)
    got = _links(out)
    assert len(got) == 1
    assert got[0].kind == links.URI
    assert got[0].uri == "mailto:sales@example.com"
    assert got[0].bbox == pytest.approx((72, 200, 300, 220), abs=0.5)


def test_add_goto_link_roundtrip(multipage_pdf, tmp_path):
    out = tmp_path / "goto.pdf"
    with PdfDocument.open(multipage_pdf) as doc:
        doc.add_link(0, (72, 200, 300, 220), dest_page=3, dest_point=(10, 20))
        doc.save(out)
    got = _links(out)
    assert len(got) == 1
    assert got[0].kind == links.GOTO
    assert got[0].dest_page == 3
    assert got[0].dest_point == pytest.approx((10, 20), abs=0.5)


def test_add_link_requires_exactly_one_target(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        with pytest.raises(ValueError):
            doc.add_link(0, (10, 10, 100, 40))  # neither
        with pytest.raises(ValueError):
            doc.add_link(0, (10, 10, 100, 40), uri="https://x", dest_page=1)  # both
        with pytest.raises(ValueError):
            doc.add_link(0, (10, 10, 100, 40), uri="   ")  # empty uri


def test_add_goto_out_of_range(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        with pytest.raises(ValueError):
            doc.add_link(0, (10, 10, 100, 40), dest_page=99)


def test_add_link_too_small(text_pdf):
    with PdfDocument.open(text_pdf) as doc:
        with pytest.raises(ValueError):
            doc.add_link(0, (10, 10, 12, 12), uri="https://x")


# --- edit target / move / resize / delete ------------------------------------


def test_update_link_uri(links_pdf, tmp_path):
    out = tmp_path / "updated.pdf"
    with PdfDocument.open(links_pdf.path) as doc:
        uri = next(i for i in doc.links(0) if i.kind == links.URI)
        doc.update_link(0, uri.xref, uri="https://new.example.com/x?y=1#f")
        doc.save(out)
    got = [i for i in _links(out) if i.kind == links.URI]
    assert got[0].uri == "https://new.example.com/x?y=1#f"


def test_update_converts_uri_to_goto(links_pdf, tmp_path):
    out = tmp_path / "converted.pdf"
    with PdfDocument.open(links_pdf.path) as doc:
        uri = next(i for i in doc.links(0) if i.kind == links.URI)
        rect = uri.bbox
        doc.update_link(0, uri.xref, dest_page=1)
        doc.save(out)
    got = _links(out)
    conv = next(i for i in got if i.bbox == pytest.approx(rect, abs=1.0))
    assert conv.kind == links.GOTO and conv.dest_page == 1


def test_move_link_roundtrip(links_pdf, tmp_path):
    out = tmp_path / "moved.pdf"
    with PdfDocument.open(links_pdf.path) as doc:
        uri = next(i for i in doc.links(0) if i.kind == links.URI)
        x0, y0, x1, y1 = uri.bbox
        doc.move_link(0, uri.xref, (15, 25))
        doc.save(out)
    got = next(i for i in _links(out) if i.kind == links.URI)
    assert got.bbox == pytest.approx((x0 + 15, y0 + 25, x1 + 15, y1 + 25), abs=0.5)


def test_resize_link_roundtrip(links_pdf, tmp_path):
    out = tmp_path / "resized.pdf"
    with PdfDocument.open(links_pdf.path) as doc:
        uri = next(i for i in doc.links(0) if i.kind == links.URI)
        doc.resize_link(0, uri.xref, (60, 90, 300, 130))
        doc.save(out)
    got = next(i for i in _links(out) if i.kind == links.URI)
    assert got.bbox == pytest.approx((60, 90, 300, 130), abs=0.5)


def test_resize_link_too_small_raises(links_pdf):
    with PdfDocument.open(links_pdf.path) as doc:
        uri = next(i for i in doc.links(0) if i.kind == links.URI)
        with pytest.raises(ValueError):
            doc.resize_link(0, uri.xref, (60, 90, 62, 92))


def test_delete_link_roundtrip(links_pdf, tmp_path):
    out = tmp_path / "deleted.pdf"
    with PdfDocument.open(links_pdf.path) as doc:
        uri = next(i for i in doc.links(0) if i.kind == links.URI)
        doc.delete_link(0, uri.xref)
        assert len(doc.links(0)) == 1  # live doc reflects the delete
        doc.save(out)
    got = _links(out)
    assert len(got) == 1 and got[0].kind == links.GOTO


# --- rotation -----------------------------------------------------------------


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_link_rect_unrotated_on_rotated_page(text_pdf, tmp_path, rotation):
    out = tmp_path / f"rot{rotation}.pdf"
    with PdfDocument.open(text_pdf) as doc:
        doc._doc[0].set_rotation(rotation)  # noqa: SLF001 - test rig
        doc.add_link(0, (30, 40, 130, 70), uri="https://rot.example")
        doc.save(out)
    doc = pymupdf.open(str(out))
    try:
        assert doc[0].rotation == rotation
        got = links.links_on_page(doc, 0)[0]
        # Stored/read back in UNROTATED page space regardless of rotation.
        assert got.bbox == pytest.approx((30, 40, 130, 70), abs=0.5)
    finally:
        doc.close()


# --- redaction guard ----------------------------------------------------------


def test_guard_preserves_link_across_text_edit(text_pdf, tmp_path):
    """Editing text under a link must not silently destroy the link."""
    out = tmp_path / "guard.pdf"
    with PdfDocument.open(text_pdf) as doc:
        span = doc.text_spans(0)[0]
        doc.add_link(0, span.bbox, uri="https://keepme.example")
        # The link rect covers the span; the edit's redaction band intersects it.
        doc.replace_text(0, span, "Rewritten heading")
        doc.save(out)
    got = _links(out)
    assert any(i.uri == "https://keepme.example" for i in got), "guard lost the link"


def test_guard_moves_link_with_paragraph(text_pdf, tmp_path):
    out = tmp_path / "guard_move.pdf"
    with PdfDocument.open(text_pdf) as doc:
        para = doc.paragraphs(0)[0]
        x0, y0, x1, y1 = para.bbox
        # A link squarely inside the paragraph box, over its text.
        link_rect = (x0 + 2, y0 + 2, x0 + 80, y1 - 2)
        doc.add_link(0, link_rect, uri="https://follow.example")
        doc.replace_paragraph(0, para, para.text, offset=(0.0, 120.0))
        doc.save(out)
    got = [i for i in _links(out) if i.uri == "https://follow.example"]
    assert got, "guard lost the moved link"
    # The link followed the +120pt vertical move.
    assert got[0].bbox[1] == pytest.approx(link_rect[1] + 120, abs=2.0)


# --- links follow their page --------------------------------------------------


def test_links_follow_page_delete(links_pdf, tmp_path):
    out = tmp_path / "pagedel.pdf"
    with PdfDocument.open(links_pdf.path) as doc:
        doc.delete([1])  # drop the middle page; page 0 keeps its links
        doc.save(out)
    got = _links(out)
    assert len(got) == 2
    goto = next(i for i in got if i.kind == links.GOTO)
    # The go-to target (was page 2) shifts to page 1 after deleting page 1.
    assert goto.dest_page == 1


def test_links_follow_page_reorder(links_pdf, tmp_path):
    out = tmp_path / "reorder.pdf"
    with PdfDocument.open(links_pdf.path) as doc:
        doc.reorder([2, 1, 0])  # page 0 (with links) moves to the end
        doc.save(out)
    doc = pymupdf.open(str(out))
    try:
        assert len(links.links_on_page(doc, 2)) == 2  # links rode along to index 2
    finally:
        doc.close()


# --- styled text links (H1) ---------------------------------------------------


def _paragraph_pdf(tmp_path, text="Please click here now"):
    p = tmp_path / "para.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=400, height=300)
    pg.insert_text((60, 100), text, fontname="helv", fontsize=12)
    d.save(str(p))
    d.close()
    return p


def _word_rect(doc, wanted):
    ws = doc._doc[0].get_text("words")  # noqa: SLF001 - test rig
    sel = [w for w in ws if w[4] in wanted]
    return (
        min(w[0] for w in sel),
        min(w[1] for w in sel),
        max(w[2] for w in sel),
        max(w[3] for w in sel),
    )


def test_word_link_blue_is_office_hyperlink():
    assert links.WORD_LINK_BLUE == 0x0563C1


def test_add_link_rects_multi_line_one_target(text_pdf, tmp_path):
    out = tmp_path / "multi.pdf"
    rects = [(60, 100, 300, 116), (60, 120, 300, 136), (60, 140, 300, 156)]
    with PdfDocument.open(text_pdf) as doc:
        xrefs = doc.add_link_rects(0, rects, uri="https://multi.example")
        assert len(xrefs) == 3
        doc.save(out)
    got = _links(out)
    assert len(got) == 3
    assert {i.uri for i in got} == {"https://multi.example"}


def test_underline_rects_draws_blue_and_keeps_links(tmp_path):
    src = _paragraph_pdf(tmp_path)
    out = tmp_path / "ul.pdf"
    with PdfDocument.open(src) as doc:
        rect = _word_rect(doc, {"click", "here"})
        doc.underline_rects(0, [rect])
        doc.add_link_rects(0, [rect], uri="https://ul.example")
        doc.save(out)
    doc = pymupdf.open(str(out))
    try:
        blues = [
            d
            for d in doc[0].get_drawings()
            if d.get("color") and abs(d["color"][2] - links.WORD_LINK_BLUE_RGB[2]) < 0.05
        ]
        assert blues, "no blue underline drawn"
        assert doc[0].get_links(), "fallback link missing"
    finally:
        doc.close()


def test_style_selection_recolors_only_selected(tmp_path):
    src = _paragraph_pdf(tmp_path)
    out = tmp_path / "styled.pdf"
    with PdfDocument.open(src) as doc:
        rect = _word_rect(doc, {"click", "here"})
        para = doc.paragraphs(0)[0]
        doc.style_paragraph_selection(0, para, [rect], color=links.WORD_LINK_BLUE)
        doc.save(out)
    with PdfDocument.open(out) as doc:
        spans = doc.text_spans(0)
        blue = [s for s in spans if s.color == links.WORD_LINK_BLUE]
        assert blue and all(s.underline for s in blue)
        joined = "".join(s.text for s in blue)
        assert "click" in joined and "here" in joined
        # The rest of the paragraph keeps its original black, un-underlined style.
        black = [s for s in spans if s.color == 0]
        assert black and not any(s.underline for s in black)
        assert "Please" in "".join(s.text for s in black)


def test_style_selection_then_link_round_trip(tmp_path):
    src = _paragraph_pdf(tmp_path)
    out = tmp_path / "styled_link.pdf"
    with PdfDocument.open(src) as doc:
        rect = _word_rect(doc, {"click", "here"})
        para = doc.paragraphs(0)[0]
        doc.style_paragraph_selection(0, para, [rect], color=links.WORD_LINK_BLUE)
        doc.add_link_rects(0, [rect], uri="https://styled.example")
        doc.save(out)
    with PdfDocument.open(out) as doc:
        assert any(s.color == links.WORD_LINK_BLUE for s in doc.text_spans(0))
        got = doc.links(0)
        assert got and got[0].uri == "https://styled.example"


def test_style_selection_no_underline(tmp_path):
    src = _paragraph_pdf(tmp_path)
    out = tmp_path / "nounderline.pdf"
    with PdfDocument.open(src) as doc:
        rect = _word_rect(doc, {"click", "here"})
        para = doc.paragraphs(0)[0]
        doc.style_paragraph_selection(0, para, [rect], color=0x008000, underline=False)
        doc.save(out)
    with PdfDocument.open(out) as doc:
        green = [s for s in doc.text_spans(0) if s.color == 0x008000]
        assert green and not any(s.underline for s in green)
