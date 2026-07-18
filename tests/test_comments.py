"""Review comments (E11): markup annotations that never flatten and never
print by default. Engine round-trips, shrinkwrap + padding geometry, callout
leader rendering (E11.5), and the exclusion and print rules."""

from __future__ import annotations

import pymupdf
import pytest

from pdfcore.comments import _BORDER_W, _PAD
from pdfcore.document import PdfDocument

RECT = (100.0, 100.0, 300.0, 140.0)  # only the top-left anchors — size is fitted


def _base_pdf(tmp_path, name="c.pdf"):
    path = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 300), "REAL page content line", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _text_width(text: str) -> float:
    return pymupdf.get_text_length(text, fontname="helv", fontsize=9)


def _ink_bbox(page, clip, zoom=3, thresh=120):
    """Bounding box of dark (text/stroke) pixels inside clip, page points."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=pymupdf.Rect(clip))
    xs, ys = [], []
    for yy in range(pix.height):
        for xx in range(pix.width):
            if min(pix.pixel(xx, yy)[:3]) < thresh:
                xs.append(xx)
                ys.append(yy)
    assert xs, "no ink found inside the clip"
    return (
        clip[0] + min(xs) / zoom,
        clip[1] + min(ys) / zoom,
        clip[0] + (max(xs) + 1) / zoom,
        clip[1] + (max(ys) + 1) / zoom,
    )


def _dark_near(pix, x, y, zoom, thresh=150, radius=2):
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            px = min(max(round(x * zoom) + dx, 0), pix.width - 1)
            py = min(max(round(y * zoom) + dy, 0), pix.height - 1)
            if min(pix.pixel(px, py)[:3]) < thresh:
                return True
    return False


def _red_near(pix, x, y, zoom, radius=2):
    """A clearly-RED pixel near (x, y) — the callout chrome colour."""
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            px = min(max(round(x * zoom) + dx, 0), pix.width - 1)
            py = min(max(round(y * zoom) + dy, 0), pix.height - 1)
            r, g, b = pix.pixel(px, py)[:3]
            if r > 150 and g < 120 and b < 120:
                return True
    return False


def _leader_fraction(page, p0, p1, zoom=3):
    """Fraction of samples along segment p0->p1 with a dark stroke nearby."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    total = 14
    hits = 0
    for i in range(1, total + 1):
        t = i / (total + 1)
        x = p0[0] + (p1[0] - p0[0]) * t
        y = p0[1] + (p1[1] - p0[1]) * t
        hits += _dark_near(pix, x, y, zoom)
    return hits / total


def _attach_midpoint(rect, target):
    """The box-edge midpoint nearest the target (mirrors the engine rule)."""
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    candidates = [(x0, cy), (x1, cy), (cx, y0), (cx, y1)]
    tx, ty = target
    return min(candidates, key=lambda p: (p[0] - tx) ** 2 + (p[1] - ty) ** 2)


def test_comment_roundtrip_metadata_and_noprint(tmp_path):
    src = _base_pdf(tmp_path)
    out = tmp_path / "saved.pdf"
    with PdfDocument.open(src) as doc:
        xref = doc.add_comment(0, RECT, "please re-check this price", author="Scott")
        assert xref > 0
        doc.save(out)

    with PdfDocument.open(out) as doc:
        comments = doc.comments(0)
        assert len(comments) == 1
        c = comments[0]
        assert c.text == "please re-check this price"
        assert c.author == "Scott"  # Acrobat shows Title as the author
        assert c.created.startswith("D:") and c.modified.startswith("D:")
        assert c.kind == "note"
        assert c.target is None
        # The rect ANCHORS at the request's top-left but shrinkwraps the text.
        assert c.rect[0] == pytest.approx(RECT[0], abs=0.5)
        assert c.rect[1] == pytest.approx(RECT[1], abs=0.5)
        expected_w = _text_width("please re-check this price") + 2 * _PAD + 1.0
        assert c.rect[2] - c.rect[0] == pytest.approx(expected_w, abs=1.5)
        assert c.rect[3] - c.rect[1] == pytest.approx(10.8 + 2 * _PAD, abs=1.5)
        # The PDF Print flag stays CLEARED: conforming viewers won't print it.
        page = doc._doc[0]
        annot = next(page.annots())
        assert not (annot.flags & pymupdf.PDF_ANNOT_IS_PRINT)


def test_comment_box_shrinkwraps_with_padding(tmp_path):
    """E11.5: the box hugs the text and the text sits _PAD in from every
    edge (pixel-verified — MuPDF glues FreeText text to the top-left; the
    padding comes from our AP nudge and must survive save/reopen)."""
    src = _base_pdf(tmp_path)
    out = tmp_path / "saved.pdf"
    with PdfDocument.open(src) as doc:
        doc.add_comment(0, RECT, "Hgpq padded note", author="Scott")
        doc.add_comment(0, (100.0, 400.0, 0.0, 0.0), "line one\nHgpq two", author="Scott")
        two = doc.comments(0)[1]
        assert two.rect[3] - two.rect[1] == pytest.approx(2 * 10.8 + 2 * _PAD, abs=1.5)
        doc.save(out)

    with PdfDocument.open(out) as doc:
        c = doc.comments(0)[0]
        ink = _ink_bbox(doc._doc[0], c.rect)
        left, top = ink[0] - c.rect[0], ink[1] - c.rect[1]
        right, bottom = c.rect[2] - ink[2], c.rect[3] - ink[3]
        assert 2.5 <= left <= 7.0, f"left padding {left}"
        assert 2.5 <= top <= 7.5, f"top padding {top}"
        assert 2.0 <= right <= 9.0, f"right padding {right}"
        assert 2.0 <= bottom <= 9.5, f"bottom padding {bottom}"


def test_callout_draws_leader_arrow_and_border(tmp_path):
    """E11.5/E11.7/E11.12: a callout must actually RENDER its leader + OPEN
    arrowhead, and its box carries a border — all RED at ``_BORDER_W``
    (user choices: triple weight, red, half-size open head — a closed
    head's fill regenerates from the box tint in Acrobat, so it went)."""
    src = _base_pdf(tmp_path)
    out = tmp_path / "saved.pdf"
    target = (80.0, 250.0)
    with PdfDocument.open(src) as doc:
        xref = doc.add_comment(0, RECT, "this bit here", author="Scott", callout_target=target)
        kind, value = doc._doc.xref_get_key(xref, "IT")
        assert kind == "name" and "Callout" in value
        doc.save(out)

    with PdfDocument.open(out) as doc:
        c = doc.comments(0)[0]
        assert c.kind == "callout"
        assert c.target == pytest.approx(target, abs=1.0)
        # rect is the TEXT BOX, not the leader-enclosing annotation union.
        assert c.rect[0] == pytest.approx(RECT[0], abs=1.5)
        assert c.rect[1] == pytest.approx(RECT[1], abs=1.5)
        assert c.rect[2] - c.rect[0] == pytest.approx(
            _text_width("this bit here") + 2 * (_PAD + _BORDER_W / 2) + 1.0, abs=2.5
        )
        page = doc._doc[0]
        # Leader: RED stroke along target -> nearest-edge attach point.
        attach = _attach_midpoint(c.rect, target)
        assert _leader_fraction(page, target, attach) >= 0.8
        pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3))
        mid = ((target[0] + attach[0]) / 2, (target[1] + attach[1]) / 2)
        assert _red_near(pix, mid[0], mid[1], 3)
        # Arrowhead: OPEN and HALVED (user choices — a closed head's fill
        # regenerates from the box tint in Acrobat, so the fill was
        # dropped). The stream is the ground truth: the head is a
        # wing→tip→wing STROKE (no close-fill op) with halved wings, and
        # each wing renders red at its midpoint.
        import re as re_module

        ap_xref = int(doc._doc.xref_get_key(c.xref, "AP/N")[1].split()[0])
        stream = doc._doc.xref_stream(ap_xref)
        m = re_module.search(
            rb"([0-9.+-]+) ([0-9.+-]+) m\n([0-9.+-]+) ([0-9.+-]+) l\n"
            rb"([0-9.+-]+) ([0-9.+-]+) l\n(S|h\nb)",
            stream,
        )
        assert m is not None and m.group(7) == b"S", "arrowhead is not an open stroke"
        w1x, w1y, tip_x, tip_y, w2x, w2y = (float(g) for g in m.groups()[:6])
        page_h = doc._doc[0].rect.height
        # MuPDF's full head is ~10× the stroke width; halved ≈ 5×.
        for wx, wy in ((w1x, w1y), (w2x, w2y)):
            length = ((wx - tip_x) ** 2 + (wy - tip_y) ** 2) ** 0.5
            assert 3.5 * _BORDER_W <= length <= 6.5 * _BORDER_W, f"wing not halved: {length:.1f}"
            mx, my = (wx + tip_x) / 2, page_h - (wy + tip_y) / 2  # y-up -> page
            assert _red_near(pix, mx, my, 3, radius=2), "wing stroke not red"
        # ...and past the halved head the leader is all that remains.
        ux, uy = attach[0] - target[0], attach[1] - target[1]
        norm = (ux**2 + uy**2) ** 0.5
        ux, uy = ux / norm, uy / norm
        for side in (3.5, -3.5):
            sx = target[0] + ux * 22.0 - uy * side
            sy = target[1] + uy * 22.0 + ux * side
            assert not _red_near(pix, sx, sy, 3, radius=1), f"arrowhead not halved ({side:+})"
        # Border: RED stroke at each box edge midpoint.
        x0, y0, x1, y1 = c.rect
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for ex, ey in [(cx, y0), (cx, y1), (x0, cy), (x1, cy)]:
            assert _red_near(pix, ex, ey, 3), f"no border stroke at ({ex}, {ey})"
        # The text itself stays BLACK (the stroke recolour must not bleed
        # into the DA text colour).
        ink = _ink_bbox(page, c.rect)
        tx_, ty_ = (ink[0] + ink[2]) / 2, (ink[1] + ink[3]) / 2
        assert _dark_near(pix, tx_, ty_, 3, thresh=100, radius=4)
        assert not _red_near(pix, tx_, ty_, 3, radius=4)
        # Notes stay borderless: their edges show only the tint.
        with PdfDocument.open(src) as plain:
            plain.add_comment(0, RECT, "this bit here", author="Scott")
            n = plain.comments(0)[0]
            npix = plain._doc[0].get_pixmap(matrix=pymupdf.Matrix(3, 3))
            nx = (n.rect[0] + n.rect[2]) / 2
            assert not _dark_near(npix, nx, n.rect[1], 3, radius=1)


def test_move_edit_delete_comment(tmp_path):
    src = _base_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        xref = doc.add_comment(0, RECT, "v1", author="Scott")
        first = doc.comments(0)[0]

        # Moves and edits RECREATE the annotation (one creation path rebuilds
        # leader/border/padding) — chain the returned xref.
        xref = doc.move_comment(0, xref, (150.0, 200.0, 350.0, 240.0))
        moved = doc.comments(0)[0]
        assert moved.rect[0] == pytest.approx(150.0, abs=0.5)
        assert moved.rect[1] == pytest.approx(200.0, abs=0.5)
        assert moved.rect[2] - moved.rect[0] == pytest.approx(
            first.rect[2] - first.rect[0], abs=1.0
        )
        assert moved.author == "Scott" and moved.created == first.created

        xref = doc.update_comment_text(0, xref, "v2 — checked")
        edited = doc.comments(0)[0]
        assert edited.text == "v2 — checked"
        assert edited.modified >= first.modified
        assert edited.author == "Scott" and edited.created == first.created
        # ...and the box re-shrinkwrapped around the longer text in place.
        assert edited.rect[0] == pytest.approx(150.0, abs=0.5)
        assert (edited.rect[2] - edited.rect[0]) > (moved.rect[2] - moved.rect[0])

        assert doc.comment_at(0, edited.rect[0] + 5.0, edited.rect[1] + 5.0) is not None
        assert doc.comment_at(0, 50.0, 50.0) is None

        doc.delete_comment(0, xref)
        assert doc.comments(0) == []


def test_edit_and_move_callout_keep_target_and_leader(tmp_path):
    src = _base_pdf(tmp_path)
    target = (80.0, 250.0)
    with PdfDocument.open(src) as doc:
        xref = doc.add_comment(0, RECT, "short", author="Scott", callout_target=target)

        xref = doc.update_comment_text(0, xref, "a much longer callout text")
        edited = doc.comments(0)[0]
        assert edited.kind == "callout"
        assert edited.target == pytest.approx(target, abs=1.0)

        xref = doc.move_comment(0, xref, (300.0, 400.0, 0.0, 0.0))
        moved = doc.comments(0)[0]
        assert moved.kind == "callout"
        assert moved.target == pytest.approx(target, abs=1.0)
        assert moved.rect[0] == pytest.approx(300.0, abs=1.0)
        # The leader re-attaches to the MOVED box.
        attach = _attach_midpoint(moved.rect, target)
        assert _leader_fraction(doc._doc[0], target, attach) >= 0.8


def test_comment_text_excluded_from_editing_surface(tmp_path):
    """Comment text leaks into get_text — the editing extraction must drop it
    (a comment is markup; it must never become an editable span/paragraph)."""
    src = _base_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        doc.add_comment(0, RECT, "COMMENT WORDS", author="Scott")
        texts = [s.text for s in doc.text_spans(0)]
        assert any("REAL page content" in t for t in texts)
        assert not any("COMMENT" in t for t in texts)
        assert doc.paragraph_at(0, 110.0, 110.0) is None  # nothing editable there


def test_comment_text_excluded_from_extract_and_search_by_default(tmp_path):
    src = _base_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        doc.add_comment(0, RECT, "COMMENT WORDS", author="Scott")

        page_text = doc.page_text(0)
        assert "REAL page content" in page_text.text
        assert "COMMENT" not in page_text.text  # Extract Text: always excluded

        assert doc.search("COMMENT WORDS") == []  # search: excluded by default
        assert len(doc.search("COMMENT WORDS", include_comments=True)) == 1
        assert len(doc.search("REAL page content")) == 1  # content unaffected


def test_print_respects_print_comments_option(qapp, tmp_path):
    """Our print path rasterizes annotations, so the option hides comments
    during the render (and restores them after)."""
    from PySide6.QtPrintSupport import QPrinter

    from pdfapp.print_support import PrintOptions, render_onto

    src = _base_pdf(tmp_path)

    def print_to(path, options):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(path))
        printer.setResolution(100)
        with PdfDocument.open(src) as doc:
            doc.add_comment(0, RECT, "COMMENT", author="Scott")
            render_onto(printer, doc, options)
            # the hide is transient: the comment is visible again afterwards
            assert doc.comments(0)[0].text == "COMMENT"

    def tint_present(path):
        """Scan the printed page for the comment's yellow tint (r high, b
        depressed) — fit-to-page scaling moves it, so search, don't sample."""
        printed = pymupdf.open(str(path))
        try:
            pix = printed[0].get_pixmap(matrix=pymupdf.Matrix(1, 1))
            for y in range(0, pix.height, 3):
                for x in range(0, pix.width, 3):
                    r, g, b = pix.pixel(x, y)[:3]
                    if r > 240 and g > 230 and b < 225:
                        return True
            return False
        finally:
            printed.close()

    off = tmp_path / "off.pdf"
    print_to(off, PrintOptions())  # default: comments do NOT print
    on = tmp_path / "on.pdf"
    print_to(on, PrintOptions(print_comments=True))
    assert not tint_present(off)
    assert tint_present(on)


def test_hidden_toggle_counts_and_preserves_appearance(tmp_path):
    src = _base_pdf(tmp_path)
    target = (60.0, 300.0)
    with PdfDocument.open(src) as doc:
        doc.add_comment(0, RECT, "one", author="A")
        doc.add_comment(0, (320.0, 100.0, 0.0, 0.0), "two", author="B", callout_target=target)
        assert doc.set_comments_hidden(True) == 2
        assert doc.set_comments_hidden(True) == 0  # already hidden
        # Hidden means GONE from the render (our print path relies on it).
        pix = doc._doc[0].get_pixmap(matrix=pymupdf.Matrix(2, 2))
        note = doc.comments(0)[0]
        cx, cy = (note.rect[0] + note.rect[2]) / 2, (note.rect[1] + note.rect[3]) / 2
        assert min(pix.pixel(round(cx * 2), round(cy * 2))[:3]) > 240
        assert doc.set_comments_hidden(False) == 2
        # The hide/show cycle must NOT regenerate appearances: the padding
        # nudge and the callout leader are still there afterwards.
        callout = doc.comments(0)[1]
        ink = _ink_bbox(doc._doc[0], doc.comments(0)[0].rect)
        assert ink[0] - doc.comments(0)[0].rect[0] >= 2.5
        attach = _attach_midpoint(callout.rect, target)
        assert _leader_fraction(doc._doc[0], target, attach) >= 0.8


def test_highlights_are_not_comments(tmp_path, quote_pdf):
    """Highlights are annotations too — the comment machinery must not
    touch them (they keep printing; they're not listed as comments)."""
    with PdfDocument.open(quote_pdf.path) as doc:
        span = next(s for s in doc.text_spans(0) if s.text.strip() == quote_pdf.price)
        doc.highlight(0, span)
        assert doc.comments(0) == []
        assert doc.set_comments_hidden(True) == 0


def test_comments_survive_text_edits_and_callout_follows_moves(tmp_path):
    """User report (2026-07-18): moving a text box a callout pointed at
    DELETED the callout — apply_redactions removes annotations whose rect
    intersects a band, and a callout's rect is the whole leader union. The
    comment guard restores them, and a MOVED box carries the target along."""
    src = _base_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        doc.add_comment(0, (400.0, 80.0, 0.0, 0.0), "note on text", author="A")
        doc.add_comment(
            0, (300.0, 150.0, 0.0, 0.0), "look here", author="B", callout_target=(150.0, 296.0)
        )
        para = doc.paragraph_at(0, 150.0, 296.0)
        assert para is not None

        # In-place EDIT: both comments survive, the target stays put.
        doc.replace_paragraph(0, para, "REAL page content line edited")
        after = doc.comments(0)
        assert len(after) == 2
        callout = next(c for c in after if c.kind == "callout")
        assert callout.target == pytest.approx((150.0, 296.0), abs=1.0)
        assert callout.author == "B" and callout.text == "look here"

        # MOVE: the arrowhead follows the text box.
        para = doc.paragraph_at(0, 150.0, 296.0)
        doc.replace_paragraph(0, para, para.text, offset=(40.0, 60.0))
        callout = next(c for c in doc.comments(0) if c.kind == "callout")
        assert callout.target == pytest.approx((190.0, 356.0), abs=1.5)
        assert callout.rect[0] == pytest.approx(300.0, abs=1.5)  # box stays put
        attach = _attach_midpoint(callout.rect, callout.target)
        assert _leader_fraction(doc._doc[0], callout.target, attach) >= 0.8
        note = next(c for c in doc.comments(0) if c.kind == "note")
        assert note.target is None and note.text == "note on text"


def test_move_comment_target_repoints_the_arrowhead(tmp_path):
    src = _base_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        xref = doc.add_comment(0, RECT, "aim", author="S", callout_target=(80.0, 250.0))
        first = doc.comments(0)[0]

        xref = doc.move_comment_target(0, xref, (300.0, 400.0))
        c = doc.comments(0)[0]
        assert c.kind == "callout"
        assert c.target == pytest.approx((300.0, 400.0), abs=1.0)
        assert c.rect[0] == pytest.approx(first.rect[0], abs=1.0)  # box stays
        assert c.text == "aim" and c.author == "S" and c.created == first.created
        attach = _attach_midpoint(c.rect, c.target)
        assert _leader_fraction(doc._doc[0], c.target, attach) >= 0.8

        nref = doc.add_comment(0, (450.0, 500.0, 0.0, 0.0), "n", author="S")
        with pytest.raises(ValueError):
            doc.move_comment_target(0, nref, (10.0, 10.0))  # notes have no arrow


def test_callout_follows_moved_image_and_survives_delete(tmp_path, sample_png):
    src = _base_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        doc.insert_image(0, (100.0, 400.0, 200.0, 480.0), sample_png)
        image = doc.images(0)[0]
        doc.add_comment(
            0, (350.0, 200.0, 0.0, 0.0), "this image", author="S", callout_target=(150.0, 440.0)
        )

        doc.move_image(0, image, (60.0, 30.0))
        c = doc.comments(0)[0]
        assert c.target == pytest.approx((210.0, 470.0), abs=1.5)  # followed the image

        image = doc.images(0)[0]
        doc.delete_image(0, image)
        c = doc.comments(0)[0]  # survives the delete; target stays where it was
        assert c.target == pytest.approx((210.0, 470.0), abs=1.5)


def test_callout_styling_survives_appearance_regeneration(tmp_path):
    """User report (2026-07-18): editing the callout in Acrobat reverted it
    to black — Acrobat REBUILDS the appearance from the annotation
    dictionary, which carried no styling. The dictionary now encodes it the
    way Acrobat does (DA = line/border colour; RC + DS = the black text;
    /C = the box fill), so a regenerated appearance keeps the red chrome.
    Simulated here with MuPDF's own C-level regenerator."""
    src = _base_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        xref = doc.add_comment(0, RECT, "styled", author="S", callout_target=(80.0, 250.0))
        raw = doc._doc
        kind, da = raw.xref_get_key(xref, "DA")
        assert kind == "string" and da.startswith("1 0 0 rg")
        kind, rc = raw.xref_get_key(xref, "RC")
        assert kind == "string" and "styled" in rc and "color:#000000" in rc
        kind, ds = raw.xref_get_key(xref, "DS")
        assert kind == "string" and "Helvetica" in ds
        # Notes stay plain — black DA, no RC (their look IS the semantics).
        nref = doc.add_comment(0, (400.0, 400.0, 0.0, 0.0), "plain", author="S")
        assert "1 0 0" not in raw.xref_get_key(nref, "DA")[1]
        assert raw.xref_get_key(nref, "RC")[0] == "null"

        # Simulate an external editor rebuilding the appearance from the
        # dictionary (what Acrobat does on edit): the chrome must stay red.
        page = raw[0]
        annot = next(a for a in page.annots() if a.xref == xref)
        pymupdf.mupdf.pdf_dirty_annot(annot.this)
        pymupdf.mupdf.pdf_update_annot(annot.this)
        c = next(cc for cc in doc.comments(0) if cc.kind == "callout")
        pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3))
        x0, y0, x1, y1 = c.rect
        assert _red_near(pix, (x0 + x1) / 2, y0, 3), "regenerated border lost the red"
        attach = _attach_midpoint(c.rect, c.target)
        assert _leader_fraction(page, c.target, attach) >= 0.7
