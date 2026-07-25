"""Engine text-span extraction + base-14 font mapping (Phase 2, E1).

Pure-helper tests, extraction against the generated quote fixture, embedded
detection, and real-sample checks. Real-sample tests skip when samples/ is
absent — the real corpus is local-only and never committed (customer data).
"""

from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

from pdfcore.document import PdfDocument
from pdfcore.textedit import (
    FLAG_BOLD,
    FLAG_ITALIC,
    SCRIPT_SUB,
    SCRIPT_SUPER,
    Paragraph,
    StyledRun,
    TextStyle,
    _embedded_font_map,
    extract_spans,
    logical_line_groups,
    map_font_to_base14,
    replace_paragraph_runs,
    srgb_to_rgb,
    strip_subset_prefix,
)


def _text_on_row(path, y, exclude=(), page_index=0):
    """Text on the baseline row at ``y``, left-to-right (spans may be split by
    a redaction/restore, so reconstruct spatially)."""
    spans = [
        s
        for s in _spans_for(path, page_index)
        if abs(s.origin[1] - y) < 1.5 and s.text not in exclude
    ]
    spans.sort(key=lambda s: s.bbox[0])
    return "".join(s.text for s in spans)


def _spans_for(path, page_index=0):
    doc = pymupdf.open(str(path))
    try:
        return extract_spans(doc, page_index)
    finally:
        doc.close()


def _find(spans, text):
    matches = [s for s in spans if s.text.strip() == text]
    assert matches, f"no span with text {text!r}"
    return matches[0]


# --- pure helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ABCDEF+Helvetica", "Helvetica"),
        ("BCDEFG+Arial-BoldMT", "Arial-BoldMT"),
        ("Helvetica", "Helvetica"),
        ("abcdef+Helvetica", "abcdef+Helvetica"),  # lowercase tag is not a subset prefix
        ("ABC+Helvetica", "ABC+Helvetica"),  # tag must be exactly six letters
    ],
)
def test_strip_subset_prefix(name, expected):
    assert strip_subset_prefix(name) == expected


@pytest.mark.parametrize(
    ("name", "flags", "expected"),
    [
        ("Helvetica", 0, "helv"),
        ("Helvetica-Bold", 0, "hebo"),
        ("Helvetica-Oblique", 0, "heit"),
        ("Helvetica-BoldOblique", 0, "hebi"),
        ("Helvetica", FLAG_BOLD, "hebo"),  # style from flag bits alone
        ("Helvetica", FLAG_BOLD | FLAG_ITALIC, "hebi"),
        ("ArialMT", 0, "helv"),
        ("Arial-BoldItalicMT", 0, "hebi"),
        ("ABCDEF+Helvetica-Bold", 0, "hebo"),  # subset tag stripped before matching
        ("Times-Roman", 0, "tiro"),
        ("TimesNewRomanPS-BoldMT", 0, "tibo"),
        ("Times-Italic", 0, "tiit"),
        ("Courier", 0, "cour"),
        ("Courier-Oblique", 0, "coit"),
        ("CourierNewPS-BoldItalicMT", 0, "cobi"),
        ("Symbol", 0, "symb"),
        ("ZapfDingbats", 0, "zadb"),
        ("Wingdings", 0, None),
        ("SomeCorporateFont", 0, None),
    ],
)
def test_map_font_to_base14(name, flags, expected):
    assert map_font_to_base14(name, flags) == expected


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        (0x000000, (0.0, 0.0, 0.0)),
        (0xFF0000, (1.0, 0.0, 0.0)),
        (0x336699, (0.2, 0.4, 0.6)),
        (0xFFFFFF, (1.0, 1.0, 1.0)),
    ],
)
def test_srgb_to_rgb(color, expected):
    assert srgb_to_rgb(color) == pytest.approx(expected)


# --- extraction: generated quote fixture ----------------------------------


def test_extract_spans_from_quote_fixture(quote_pdf):
    spans = _spans_for(quote_pdf.path)

    price = _find(spans, quote_pdf.price)
    assert price.page_index == 0
    assert price.font == "Helvetica"
    assert price.base14 == "helv"
    assert not price.embedded
    assert price.size == pytest.approx(9, abs=0.1)
    assert price.color == 0x000000

    x0, y0, x1, y1 = price.bbox
    assert x0 < x1 and y0 < y1
    ox, oy = price.origin
    assert ox == pytest.approx(x0, abs=1.0)  # origin = baseline of first glyph
    assert y0 < oy <= y1

    heading = _find(spans, quote_pdf.heading)
    assert heading.font == "Helvetica-Bold"
    assert heading.base14 == "hebo"

    red = _find(spans, quote_pdf.red_text)
    assert red.color == 0xFF0000
    assert srgb_to_rgb(red.color) == pytest.approx((1.0, 0.0, 0.0))

    assert all(not s.embedded for s in spans)  # base-14 fixture: nothing embedded


def test_abutting_spans_stay_separate(quote_pdf):
    """The bold label and regular value on one line extract as two spans."""
    spans = _spans_for(quote_pdf.path)
    label = _find(spans, quote_pdf.terms_label)
    value = _find(spans, quote_pdf.terms_value)
    assert label.font == "Helvetica-Bold"
    assert value.font == "Helvetica"
    # Same line (vertical overlap), value abutting to the label's right.
    assert label.bbox[1] < value.bbox[3] and value.bbox[1] < label.bbox[3]
    assert value.bbox[0] >= label.bbox[2] - 1.0


def test_flags_probe_bold_bit(quote_pdf):
    """Settles the span-flags bit table: hebo-inserted text reports bold=16.

    map_font_to_base14 must recover "hebo" from the flags alone (name given as
    plain "Helvetica") — the fallback path for fonts whose style is not in the
    name.
    """
    spans = _spans_for(quote_pdf.path)
    heading = _find(spans, quote_pdf.heading)
    assert heading.flags & FLAG_BOLD
    assert map_font_to_base14("Helvetica", heading.flags) == "hebo"


def test_text_spans_via_pdfdocument(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        spans = doc.text_spans(0)
    price = _find(spans, quote_pdf.price)
    assert price.base14 == "helv"


class _FakeDoc:
    """Stub exposing get_page_fonts rows, to drive the collision logic."""

    def __init__(self, rows):
        self._rows = rows

    def get_page_fonts(self, page_index):
        return self._rows


@pytest.mark.parametrize("embedded_first", [True, False])
def test_embedded_map_collision_resolves_to_embedded(embedded_first):
    """Same stripped basefont as embedded subset AND non-embedded on one page.

    Span font names never carry the subset prefix (1.28.0), so nothing
    span-side can break the tie — the map must mark the name embedded
    regardless of get_page_fonts row order (conservative: flag, never silently
    exact-match embedded glyphs).
    """
    subset = (9, "ttf", "TrueType", "ABCDEF+Helvetica", "F1", "")
    plain = (12, "n/a", "Type1", "Helvetica", "F2", "")
    rows = [subset, plain] if embedded_first else [plain, subset]
    assert _embedded_font_map(_FakeDoc(rows), 0) == {"Helvetica": True}


def test_embedded_font_flagged_not_mapped(embedded_font_pdf):
    spans = _spans_for(embedded_font_pdf)
    # Real-world artifact: the embedded TTF's spaces extract as U+00A0, so
    # normalize before matching (exactly the noise real files bring).
    target = next(s for s in spans if s.text.replace("\xa0", " ").strip() == "Embedded font text")
    assert target.font == "ArialMT"
    assert target.embedded
    assert target.base14 is None  # best-effort branch: flag, never exact-match


# --- replace op (E2) --------------------------------------------------------


def _page_text(path, page_index=0):
    doc = pymupdf.open(str(path))
    try:
        return doc[page_index].get_text()
    finally:
        doc.close()


def _replace_and_reopen(src_path, tmp_path, target_text, new_text, page_index=0):
    """Round-trip helper: open → replace → save → return (saved path, result)."""
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(src_path) as doc:
        span = _find(doc.text_spans(page_index), target_text)
        result = doc.replace_text(page_index, span, new_text)
        doc.save(out)
    return out, result


def _drawing_items(path, page_index=0):
    """Multiset of normalized vector items on a page (for survival subset checks).

    Coordinates are rounded: apply_redactions rewrites the content stream, so
    surviving items may re-serialize with different float precision.
    """
    from collections import Counter

    def norm(value):
        if isinstance(value, pymupdf.Point):
            return ("P", round(value.x, 1), round(value.y, 1))
        if isinstance(value, pymupdf.Rect):
            corners = (value.x0, value.y0, value.x1, value.y1)
            return ("R", *(round(c, 1) for c in corners))
        if isinstance(value, pymupdf.Quad):
            return ("Q", norm(value.ul), norm(value.lr))
        return value

    doc = pymupdf.open(str(path))
    try:
        items = []
        for drawing in doc[page_index].get_drawings():
            for item in drawing["items"]:
                items.append(tuple(norm(v) for v in item))
        return Counter(items)
    finally:
        doc.close()


def _is_dark_at(path, x_pt, y_pt, zoom=2.0):
    """True if any pixel in a 3x3 window around the point renders dark."""
    doc = pymupdf.open(str(path))
    try:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        px, py = round(x_pt * zoom), round(y_pt * zoom)
        darkest = 255
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                xx = min(max(px + dx, 0), pix.width - 1)
                yy = min(max(py + dy, 0), pix.height - 1)
                darkest = min(darkest, max(pix.pixel(xx, yy)))
        return darkest < 128
    finally:
        doc.close()


def test_replace_price_roundtrip(quote_pdf, tmp_path):
    out, result = _replace_and_reopen(quote_pdf.path, tmp_path, quote_pdf.price, "$1,499.00")

    assert result.inserted
    assert result.used_font == "helv"
    assert result.exact_font
    text = _page_text(out)
    assert "$1,499.00" in text
    assert quote_pdf.price not in text

    # The reinserted span carries the original size/colour and maps back.
    new_span = _find(_spans_for(out), "$1,499.00")
    assert new_span.base14 == "helv"
    assert new_span.size == pytest.approx(9, abs=0.1)
    assert new_span.color == 0x000000

    # Every OTHER cell survives — same-row, same-column and diagonal
    # neighbours at realistic table distances.
    for survivor in (
        quote_pdf.total,
        quote_pdf.date,
        quote_pdf.address,
        quote_pdf.heading,
        "C-Thru Separator",
        "GST",
    ):
        assert survivor in text, f"neighbour {survivor!r} was eaten by the redaction"


def test_replace_rejects_multiline_text(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        with pytest.raises(ValueError, match="single line"):
            doc.replace_text(0, span, "line one\nline two")


def test_replace_preserves_abutting_neighbour(quote_pdf, tmp_path):
    """The inset redact must not eat the same-line span it abuts."""
    out, _ = _replace_and_reopen(quote_pdf.path, tmp_path, quote_pdf.terms_label, "Cond:")
    text = _page_text(out)
    assert "Cond:" in text
    assert quote_pdf.terms_value in text  # abutting neighbour survived
    assert quote_pdf.terms_label not in text


def test_replace_preserves_gridlines_and_logo(quote_pdf, tmp_path):
    """Editing a table value must leave line-art and the logo image intact."""
    items_before = _drawing_items(quote_pdf.path)
    src_doc = pymupdf.open(str(quote_pdf.path))
    try:
        images_before = len(src_doc[0].get_images(full=True))
    finally:
        src_doc.close()
    assert items_before and images_before == 1

    out, _ = _replace_and_reopen(quote_pdf.path, tmp_path, quote_pdf.price, "$1,499.00")

    # Every original vector item survives (the redaction FILL adds a new white
    # rect, so counts grow — assert survival as a subset, not equality).
    missing = items_before - _drawing_items(out)
    assert not missing, f"line-art removed by redaction: {sorted(missing)}"
    edited = pymupdf.open(str(out))
    try:
        assert len(edited[0].get_images(full=True)) == images_before
    finally:
        edited.close()

    # Pixel probes: the price cell is row 1, col 2 of the fixture table. Its
    # bottom gridline and right gridline must still render dark.
    cell_x0 = quote_pdf.table_x0 + 2 * quote_pdf.cell_w
    cell_y1 = quote_pdf.table_y0 + 2 * quote_pdf.cell_h
    assert _is_dark_at(out, cell_x0 + quote_pdf.cell_w / 2, cell_y1)  # bottom edge
    assert _is_dark_at(out, cell_x0 + quote_pdf.cell_w, cell_y1 - quote_pdf.cell_h / 2)
    # Logo (grey pixmap at (40,30)-(160,110)) still renders.
    assert _is_dark_at(out, 100, 70)


def test_replace_overflow_flag(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        assert not doc.replace_text(0, span, "$9.00").overflow
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        wide = doc.replace_text(0, span, "$1,234,567,890.00 plus all surcharges")
        assert wide.overflow


def test_replace_embedded_font_best_effort(embedded_font_pdf, tmp_path):
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(embedded_font_pdf) as doc:
        span = next(
            s
            for s in doc.text_spans(0)
            if s.text.replace("\xa0", " ").strip() == "Embedded font text"
        )
        result = doc.replace_text(0, span, "Replacement text")
        doc.save(out)

    assert result.used_font == "helv"  # best effort
    assert not result.exact_font  # UI must warn: font can't be matched exactly
    text = _page_text(out)
    assert "Replacement text" in text
    assert "Embedded" not in text


# --- embedded-font reuse (Word-export editing; the "grows into occupied
# space" bug on unchanged text) ---------------------------------------------


_HELV_CODES = ("helv", "hebo", "heit", "hebi")


def _para_runs(para, *, embed=True, text_map=None):
    """Engine runs mirroring the UI's _runs_from_paragraph: per-span, with an
    embed_name intent for embedded non-base-14 spans (when ``embed``) and a
    WEIGHT-CORRECT base-14 fallback code (the engine pairs a run with the
    matching embedded weight via that code)."""
    runs = []
    for i, line in enumerate(para.lines):
        if i:
            runs.append(StyledRun("\n", TextStyle()))
        for span in line:
            reuse = embed and span.embedded and map_font_to_base14(span.font, span.flags) is None
            idx = (1 if span.flags & FLAG_BOLD else 0) + (2 if span.flags & FLAG_ITALIC else 0)
            text = span.text if text_map is None else text_map(span.text)
            runs.append(
                StyledRun(
                    text,
                    TextStyle(
                        code=span.base14 or _HELV_CODES[idx],
                        size=span.size,
                        color=span.color,
                        embed_name=span.font if reuse else None,
                    ),
                )
            )
    return runs


def test_embedded_nonstandard_font_reused_on_edit(embedded_nonstandard_font_pdf, tmp_path):
    """Editing an embedded no-base-14-mapping font reuses the DOCUMENT's own
    font (not a helv substitute) and round-trips with the text changed."""
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(embedded_nonstandard_font_pdf) as doc:
        para = next(p for p in doc.paragraphs(0) if "stretches" in p.text)
        runs = _para_runs(para, embed=True, text_map=lambda t: t.replace("stretches", "reaches"))
        result = doc.replace_paragraph_runs(0, para, runs)
        doc.save(out)
    assert not result.font_fallback  # "reaches" uses only in-subset glyphs
    text = _page_text(out)
    assert "reaches" in text and "stretches" not in text
    # The document's own embedded font (Calibri/Verdana) is re-embedded, not helv.
    fonts = {f[3] for f in pymupdf.open(str(out))[0].get_fonts()}
    assert any("Calibri" in n or "Verdana" in n for n in fonts)


def test_embedded_font_no_phantom_wrap_line(embedded_nonstandard_font_pdf):
    """The mechanism behind the bug: a full-width embedded line re-laid in helv
    wraps to an extra line (~9% wider); reusing the embedded font does not."""
    with PdfDocument.open(embedded_nonstandard_font_pdf) as doc:
        para = next(p for p in doc.paragraphs(0) if "stretches" in p.text)
        n_before = len(para.lines)
        with PdfDocument.open(embedded_nonstandard_font_pdf) as doc_helv:
            para_h = next(p for p in doc_helv.paragraphs(0) if "stretches" in p.text)
            helv = doc_helv.replace_paragraph_runs(0, para_h, _para_runs(para_h, embed=False))
        emb = doc.replace_paragraph_runs(0, para, _para_runs(para, embed=True))
    assert len(emb.visual_lines) == n_before  # faithful — no phantom line
    assert len(helv.visual_lines) > n_before  # the substitute manufactures one


def test_embedded_font_missing_glyph_falls_back(real_embedded_bug_pdf, tmp_path):
    """A character the real Word subset lacks (its Aptos subset omits q/z/...)
    falls back to the base-14 code — the text stays intact (no NUL corruption)
    and font_fallback is reported. Uses the real sample: a PyMuPDF-subsetted
    synthetic font strips the Unicode cmap, so glyph coverage can't be probed
    the way a genuine Word subset (which keeps its cmap) can."""
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(real_embedded_bug_pdf) as doc:
        para = next(p for p in doc.paragraphs(0) if p.embedded)
        runs = [StyledRun("quiz zap", TextStyle(code="helv", size=para.size, embed_name=para.font))]
        result = doc.replace_paragraph_runs(0, para, runs)
        doc.save(out)
    assert result.font_fallback  # q and z are not in the Aptos subset
    assert "quiz zap" in _page_text(out)  # every character survived extraction


def test_real_embedded_bug_paragraphs_edit_without_growth_error(real_embedded_bug_pdf):
    """The exact reported regression: every embedded multi-line paragraph in the
    Word-export sample failed to re-insert its OWN text (helv over-measured and
    tripped the growth-collision refusal). With embedded-font reuse they all
    succeed."""
    with PdfDocument.open(real_embedded_bug_pdf) as doc:
        paras = [p for p in doc.paragraphs(0) if p.embedded and len(p.lines) >= 2]
        assert paras, "expected embedded multi-line paragraphs in the sample"
        for para in paras:
            # Re-insert unchanged text on a fresh copy (mutation is destructive).
            with PdfDocument.open(real_embedded_bug_pdf) as fresh:
                target = next(p for p in fresh.paragraphs(0) if p.text == para.text)
                result = fresh.replace_paragraph_runs(0, target, _para_runs(target, embed=True))
                assert not result.font_fallback  # unchanged text is fully covered


def test_real_embedded_bug_helv_path_still_refuses(real_embedded_bug_pdf):
    """Control: WITHOUT embedded-font reuse (the old behaviour) at least one of
    those paragraphs still trips the growth refusal — confirms the sample
    reproduces the bug and the reuse is what fixes it."""
    refused = 0
    with PdfDocument.open(real_embedded_bug_pdf) as doc:
        targets = [p for p in doc.paragraphs(0) if p.embedded and len(p.lines) >= 2]
    for para in targets:
        with PdfDocument.open(real_embedded_bug_pdf) as fresh:
            target = next(p for p in fresh.paragraphs(0) if p.text == para.text)
            try:
                fresh.replace_paragraph_runs(0, target, _para_runs(target, embed=False))
            except ValueError:
                refused += 1
    assert refused > 0


# --- paragraph reflow (the "change the font -> grows into occupied space" bug:
# the editor used to freeze every wrapped line as a hard break) -----------------


def _para_pdf(tmp_path, lines, *, x0=72.0, y0=100.0, size=11.0, pitch=14.0, fontname="helv"):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for i, text in enumerate(lines):
        page.insert_text((x0, y0 + i * pitch), text, fontname=fontname, fontsize=size)
    path = tmp_path / "para.pdf"
    doc.save(str(path))
    doc.close()
    return path


def _reflowed_runs(para, code="helv"):
    """Runs the paragraph EDITOR produces after a font change: one logical group
    per reflowable line (soft-joined), the WHOLE paragraph re-styled."""
    runs = []
    for gi, group in enumerate(logical_line_groups(para)):
        if gi:
            runs.append(StyledRun("\n", TextStyle()))
        for li, line in enumerate(group):
            if li:
                prev = runs[-1].text if runs else ""
                lead = line[0].text[:1] if line and line[0].text else ""
                if prev and not prev[-1:].isspace() and not lead[:1].isspace():
                    runs.append(StyledRun(" ", runs[-1].style))
            for span in line:
                runs.append(StyledRun(span.text, TextStyle(code=code, size=span.size)))
    return runs


def test_logical_line_groups_reflows_wide_prose(tmp_path):
    lines = [
        "This is a wide flowing prose paragraph that fills the whole text column here now today",
        "and it continues onto a second line that also runs the full width of the text column",
        "before ending on a shorter third and final line.",
    ]
    src = _para_pdf(tmp_path, lines)
    with PdfDocument.open(src) as doc:
        para = doc.paragraphs(0)[0]
        assert len(para.lines) == 3
        assert len(logical_line_groups(para)) == 1  # one reflowable logical line


def test_logical_line_groups_preserves_narrow_column(tmp_path):
    # A totals column: distinct values, each intentionally on its own line.
    src = _para_pdf(tmp_path, ["1,210.47", "200.00", "$1,410.47"], x0=500.0, size=8.0, pitch=8.0)
    with PdfDocument.open(src) as doc:
        para = doc.paragraphs(0)[0]
        groups = logical_line_groups(para)
    assert len(groups) == len(para.lines)  # narrow block -> every line preserved


def _frozen_runs(para, code="helv"):
    """The OLD editor behaviour: a hard break per wrapped visual line."""
    runs = []
    for i, line in enumerate(para.lines):
        if i:
            runs.append(StyledRun("\n", TextStyle()))
        for span in line:
            runs.append(StyledRun(span.text, TextStyle(code=code, size=span.size)))
    return runs


def test_font_change_reflows_within_box(real_embedded_bug_pdf):
    """The reported bug: changing the font of a wide Word-export paragraph hit
    the growth refusal because the editor froze every wrapped line as a hard
    break. Reflow re-wraps the whole flow within the box. At least one wide
    paragraph reproduces the frozen failure; reflow succeeds on ALL of them
    (if it raised, the loop would error out)."""
    with PdfDocument.open(real_embedded_bug_pdf) as doc:
        wide = [
            p.text
            for p in doc.paragraphs(0)
            if len(p.lines) >= 2 and (p.bbox[2] - p.bbox[0]) >= 200
        ]
    assert wide, "expected wide multi-line paragraphs in the sample"
    frozen_failures = 0
    for para_text in wide:
        with PdfDocument.open(real_embedded_bug_pdf) as doc:
            para = next(p for p in doc.paragraphs(0) if p.text == para_text)
            try:
                doc.replace_paragraph_runs(0, para, _frozen_runs(para, code="helv"))
            except ValueError:
                frozen_failures += 1
        with PdfDocument.open(real_embedded_bug_pdf) as doc:
            para = next(p for p in doc.paragraphs(0) if p.text == para_text)
            doc.replace_paragraph_runs(0, para, _reflowed_runs(para, code="helv"))  # must not raise
    assert frozen_failures > 0  # the frozen-line bug reproduces


# --- list recognition (L1): bullet folding + hanging-indent editing ----------


def _bullet_list_pdf(tmp_path):
    """A bulleted list: a bullet span at x=90 sharing the body's baseline, the
    body hanging at x=108. Two items, the first wrapping to a second line."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((90, 100), "•", fontsize=11)
    page.insert_text(
        (108, 100), "First item body running fairly wide across the column here now", fontsize=11
    )
    page.insert_text((108, 116), "and wrapping onto a second line of the same item.", fontsize=11)
    page.insert_text((90, 150), "•", fontsize=11)
    page.insert_text((108, 150), "Second short item.", fontsize=11)
    path = tmp_path / "bullets.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_bullet_items_fold_into_one_paragraph(tmp_path):
    with PdfDocument.open(_bullet_list_pdf(tmp_path)) as doc:
        paras = doc.paragraphs(0)
        items = [p for p in paras if p.hang_indent > 0]
    assert len(items) == 2  # two grouped bullet items, not 4 stray fragments
    first = next(p for p in items if "First item" in p.text)
    assert first.text.lstrip().startswith(("•", "·"))  # marker at the front
    assert first.hang_indent == pytest.approx(18.0, abs=1.0)
    assert len(first.lines) == 2  # bullet folded onto the body, body wraps once


def test_bullet_item_hit_test_includes_marker(tmp_path):
    with PdfDocument.open(_bullet_list_pdf(tmp_path)) as doc:
        # a click on the body resolves the WHOLE item (marker + body)
        para = doc.paragraph_at(0, 150, 148)
    assert para is not None
    assert para.hang_indent > 0
    assert "Second short item" in para.text


def test_edit_bullet_item_preserves_marker_and_hang(tmp_path):
    src = _bullet_list_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        para = next(p for p in doc.paragraphs(0) if "First item" in p.text)
        runs = _para_runs(para, embed=False, text_map=lambda t: t.replace("First", "Edited"))
        doc.replace_paragraph_runs(0, para, runs)
        out = tmp_path / "edited.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        item = next((p for p in doc.paragraphs(0) if "Edited item" in p.text), None)
        assert item is not None  # edit applied
        assert item.hang_indent > 0  # STILL grouped (cross-block fold) with a hang
        assert item.text.lstrip().startswith(("•", "·"))  # marker survived
        # the marker sits at the box left, the body hangs deeper
        assert item.lines[0][0].bbox[0] == pytest.approx(90.0, abs=1.5)
        body = next(s for s in item.lines[0] if s.text.strip() and s.bbox[0] > 100)
        assert body.bbox[0] == pytest.approx(108.0, abs=1.5)


def test_numbered_inline_marker_not_folded(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "1. An inline numbered heading on one line.", fontsize=11)
    path = tmp_path / "num.pdf"
    doc.save(str(path))
    doc.close()
    with PdfDocument.open(path) as pdoc:
        para = pdoc.paragraphs(0)[0]
    assert para.hang_indent == 0.0  # inline number, no hanging indent to reproduce
    assert para.text.strip().startswith("1.")


_LIST_SAMPLES = [
    Path(__file__).resolve().parents[1] / "samples" / name
    for name in ("document_with_hyperlink.pdf", "sample_lists.pdf")
]


def test_real_sample_bullets_grouped():
    """The real Word-export samples' bulleted lists group into hanging-indent
    items. Skips when the (local-only) samples are absent."""
    present = [p for p in _LIST_SAMPLES if p.exists()]
    if not present:
        pytest.skip("no list sample present (samples/ is local-only)")
    for path in present:
        with PdfDocument.open(path) as doc:
            grouped = [
                p for n in range(doc.page_count) for p in doc.paragraphs(n) if p.hang_indent > 0
            ]
        assert grouped, f"expected grouped bullet items in {path.name}"
        for item in grouped:
            assert item.text.lstrip().startswith(("•", "·"))
            assert item.hang_indent == pytest.approx(18.0, abs=4.0)


def test_replace_with_empty_string_deletes(quote_pdf, tmp_path):
    out, result = _replace_and_reopen(quote_pdf.path, tmp_path, quote_pdf.date, "")
    assert not result.inserted
    assert not result.overflow
    assert quote_pdf.date not in _page_text(out)


def test_replace_preserves_vertically_overlapping_lines(tmp_path):
    """Tight leading: line bboxes overlap, editing the middle must not eat
    the lines above/below (the manual-pass bug — the sample quote's
    description cell overlaps by 2.39 pt)."""
    src = tmp_path / "tight.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    # 8pt Helvetica has ~8.9pt-tall line boxes; an 8pt pitch overlaps them.
    page.insert_text((72, 100), "LINE-ABOVE text", fontsize=8)
    page.insert_text((72, 108), "LINE-MIDDLE text", fontsize=8)
    page.insert_text((72, 116), "LINE-BELOW text", fontsize=8)
    doc.save(str(src))
    doc.close()

    out, _ = _replace_and_reopen(src, tmp_path, "LINE-MIDDLE text", "REPLACED text")
    text = _page_text(out)
    assert "REPLACED text" in text
    assert "LINE-MIDDLE" not in text
    assert "LINE-ABOVE text" in text  # fully intact, not clipped
    assert "LINE-BELOW text" in text


def test_replace_real_quote_preserves_overlapping_description_line(real_quote_pdf, tmp_path):
    """Exact manual-pass repro: edit 'International Bank or Wire ' — the
    description line above overlaps its bbox and must survive whole."""
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(real_quote_pdf) as doc:
        span = next(s for s in doc.text_spans(0) if "International Bank" in s.text)
        doc.replace_text(0, span, "International Bank or Wire Fees. Extra")
        doc.save(out)

    text = _page_text(out)
    assert "International Bank or Wire Fees. Extra" in text
    # The overlapping line above keeps its head AND tail.
    assert "hose clamp (2)" in text
    assert "service instructions" in text


def test_replace_on_rotated_page(quote_pdf, tmp_path):
    """Extraction, redaction and reinsert all speak unrotated page space."""
    out = tmp_path / "rotated_edit.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.rotate([0], 90)
        span = _find(doc.text_spans(0), quote_pdf.price)
        doc.replace_text(0, span, "$7,777.77")
        doc.save(out)

    text = _page_text(out)
    assert "$7,777.77" in text
    assert quote_pdf.price not in text
    reopened = pymupdf.open(str(out))
    try:
        assert reopened[0].rotation == 90
    finally:
        reopened.close()


def test_replace_real_quote_preserves_table_and_logo(real_quote_pdf, tmp_path):
    """Edit the PR EA price on the real quote: gridlines, logo and the other
    price occurrences all survive; only the edited occurrence changes."""
    items_before = _drawing_items(real_quote_pdf)
    src = pymupdf.open(str(real_quote_pdf))
    try:
        images_before = len(src[0].get_images(full=True))
    finally:
        src.close()
    assert images_before >= 1

    # _find returns the first "1,185.47" span (PR EA column, bbox x 637.9-669).
    out, result = _replace_and_reopen(real_quote_pdf, tmp_path, "1,185.47", "1,499.00")
    assert result.inserted and result.used_font == "helv" and result.exact_font

    missing = items_before - _drawing_items(out)
    assert not missing, f"line-art removed by redaction: {sorted(missing)}"
    edited = pymupdf.open(str(out))
    try:
        page = edited[0]
        assert len(page.get_images(full=True)) == images_before
        text = page.get_text()
    finally:
        edited.close()

    assert text.count("1,499.00") == 1  # edited occurrence
    assert text.count("1,185.47") == 2  # the other two columns untouched

    # Pixel probes on the PR EA cell borders (empirically: the cell body is the
    # stroked white-filled rect (624, 267)-(673, 485); price row y ~268.8-276.8).
    assert _is_dark_at(out, 624.0, 272.8)  # left border, crossing the price row
    assert _is_dark_at(out, 673.0, 272.8)  # right border
    assert _is_dark_at(out, 648.5, 267.0)  # header/body horizontal line above


# --- paragraph edit (E4.5) ---------------------------------------------------


def _paragraph_fixture(tmp_path):
    """Header line, then a 3-line tight-pitch body, then a separate footer.

    Mirrors the real quote's description cell: MuPDF groups the header with
    the body in one dict block (close spacing), so the pitch-run grouping has
    real work to do. The footer is far enough away to be its own block.
    """
    path = tmp_path / "paragraphs.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "HEADER row", fontname="hebo", fontsize=8)
    page.insert_text((72, 111), "body first line of the paragraph", fontsize=8)
    page.insert_text((72, 119), "body second line with more words", fontsize=8)
    page.insert_text((72, 127), "body third and final line", fontsize=8)
    page.insert_text((72, 180), "FOOTER far below", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def test_paragraph_at_groups_uniform_pitch_run(tmp_path):
    path = _paragraph_fixture(tmp_path)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)  # point on the second body line
        assert para is not None
        assert para.text.splitlines() == [
            "body first line of the paragraph",
            "body second line with more words",
            "body third and final line",
        ]
        assert "HEADER" not in para.text  # different pitch — excluded
        assert para.pitch == pytest.approx(8.0, abs=0.1)
        assert para.base14 == "helv"
        assert para.uniform_style

        header = doc.paragraph_at(0, 100, 98)  # point on the header line
        assert header is not None
        assert header.text == "HEADER row"

        assert doc.paragraph_at(0, 400, 400) is None  # empty area


def test_paragraphs_on_page_partitions_header_body_footer(tmp_path):
    path = _paragraph_fixture(tmp_path)
    with PdfDocument.open(path) as doc:
        texts = [p.text for p in doc.paragraphs(0)]
        assert texts == [
            "HEADER row",
            "body first line of the paragraph\n"
            "body second line with more words\n"
            "body third and final line",
            "FOOTER far below",
        ]


def test_paragraphs_on_page_matches_paragraph_at_pointwise(tmp_path):
    """The bulk partition and the interactive hit-test are ONE semantics."""
    path = _paragraph_fixture(tmp_path)
    with PdfDocument.open(path) as doc:
        paras = doc.paragraphs(0)
        assert paras
        for para in paras:
            for span in para.spans:
                cx = (span.bbox[0] + span.bbox[2]) / 2
                cy = (span.bbox[1] + span.bbox[3]) / 2
                assert doc.paragraph_at(0, cx, cy) == para


def test_paragraphs_on_page_quote_table_rows_stay_separate(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        paras = doc.paragraphs(0)
        x0, y0 = quote_pdf.table_x0, quote_pdf.table_y0
        x1 = x0 + quote_pdf.cols * quote_pdf.cell_w
        y1 = y0 + quote_pdf.rows * quote_pdf.cell_h
        in_table = [
            p
            for p in paras
            if p.bbox[0] >= x0 - 1
            and p.bbox[2] <= x1 + 1
            and p.bbox[1] >= y0 - 1
            and p.bbox[3] <= y1 + 1
        ]
        assert in_table  # the table's text is covered by the partition
        for p in in_table:
            assert p.bbox[3] - p.bbox[1] <= quote_pdf.cell_h  # no cross-row merge


def _fake_line(y: float) -> dict:
    return {"spans": [{"origin": (0.0, y)}]}


def test_pitch_run_same_baseline_fragments_never_join():
    """CAD exporters write a section line's END LABELS as separate dict
    "lines" on ONE baseline (delta ~0). A real paragraph advance is never
    near zero, so those must always break instead of merging."""
    from pdfcore.textedit import _pitch_run

    lines = [_fake_line(127.6), _fake_line(127.6)]
    assert _pitch_run(lines, 0) == [0]
    assert _pitch_run(lines, 1) == [1]

    # A whole title-block row of same-baseline cell labels.
    lines = [_fake_line(441.4) for _ in range(4)]
    assert all(_pitch_run(lines, i) == [i] for i in range(4))


def test_pitch_run_reference_comes_from_real_advances_only():
    from pdfcore.textedit import _pitch_run

    # Same-baseline pair above a real 8pt-pitch body: the zero delta must
    # not drag the median down. The SECOND label sits one real advance
    # above the body, so it reads as the body's first line (ambiguous and
    # accepted); the first label always stands alone.
    lines = [_fake_line(100.0), _fake_line(100.0), _fake_line(108.0), _fake_line(116.0)]
    assert _pitch_run(lines, 0) == [0]
    assert _pitch_run(lines, 2) == [1, 2, 3]
    assert _pitch_run(lines, 3) == [1, 2, 3]


# --- rotated text (CAD dimension labels) -------------------------------------


def _rotated_pdf(tmp_path):
    """One horizontal control plus quarter-turn rotated spans; V2 sits close
    to R90 so the rotated redact band's neighbour-safety is observable."""
    path = tmp_path / "rotated.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 300), "H0", fontsize=14)
    page.insert_text((200, 300), "R90", fontsize=14, rotate=90)
    page.insert_text((212, 300), "V2", fontsize=14, rotate=90)
    page.insert_text((300, 300), "R180", fontsize=14, rotate=180)
    page.insert_text((400, 300), "R270", fontsize=14, rotate=270)
    doc.save(str(path))
    doc.close()
    return path


def test_rotation_detected_on_extraction(tmp_path):
    with PdfDocument.open(_rotated_pdf(tmp_path)) as doc:
        rotations = {s.text: s.rotation for s in doc.text_spans(0)}
        assert rotations == {"H0": 0, "R90": 90, "V2": 90, "R180": 180, "R270": 270}


def test_rotated_span_roundtrip_keeps_rotation_and_removes_old(tmp_path):
    path = _rotated_pdf(tmp_path)
    out = tmp_path / "edited.pdf"
    for target, new in (("R90", "N90"), ("R180", "N180"), ("R270", "N270")):
        with PdfDocument.open(path) as doc:
            span = next(s for s in doc.text_spans(0) if s.text == target)
            result = doc.replace_text(0, span, new)
            assert result.inserted
            doc.save(out)
        with PdfDocument.open(out) as doc:
            spans = {s.text: s for s in doc.text_spans(0)}
            assert target not in spans  # the old glyphs are GONE (no leftovers)
            assert new in spans
            assert spans[new].rotation == span.rotation  # still rotated
            assert spans[new].origin[0] == pytest.approx(span.origin[0], abs=0.5)
            assert spans[new].origin[1] == pytest.approx(span.origin[1], abs=0.5)
            assert "H0" in spans  # horizontal neighbour untouched
            assert "V2" in spans  # the adjacent vertical label survives the band


def test_rotated_paragraphs_are_singletons_and_paragraph_ops_refused(tmp_path):
    with PdfDocument.open(_rotated_pdf(tmp_path)) as doc:
        span = next(s for s in doc.text_spans(0) if s.text == "R90")
        cx = (span.bbox[0] + span.bbox[2]) / 2
        cy = (span.bbox[1] + span.bbox[3]) / 2
        para = doc.paragraph_at(0, cx, cy)
        assert para is not None
        assert para.text == "R90"  # NOT merged with the vertical neighbour
        assert para.spans[0].rotation == 90
        with pytest.raises(ValueError):
            doc.replace_paragraph(0, para, "new")


def test_unsupported_angle_refused_before_mutation(tmp_path):
    import dataclasses

    with PdfDocument.open(_rotated_pdf(tmp_path)) as doc:
        span = next(s for s in doc.text_spans(0) if s.text == "R90")
        weird = dataclasses.replace(span, rotation=None)
        before = doc._doc[0].get_text()
        with pytest.raises(ValueError):
            doc.replace_text(0, weird, "X")
        assert doc._doc[0].get_text() == before  # nothing was redacted


def test_rotated_underline_draws_from_run_layout(tmp_path):
    with PdfDocument.open(_rotated_pdf(tmp_path)) as doc:
        span = next(s for s in doc.text_spans(0) if s.text == "R90")
        before = len(doc._doc[0].get_drawings())
        doc.replace_text(0, span, "U90", style=TextStyle(size=14.0, underline=True))
        assert len(doc._doc[0].get_drawings()) > before


def test_real_cad_section_labels_are_separate_paragraphs(real_cad_pdf):
    """CAD gate: 'A ... A' section-line end labels must never merge.

    Merged, the "paragraph" spanned the whole drawing (407pt-wide bbox for
    two 13pt glyphs) and reveal/select/edit treated both labels as one
    entity. Also asserts the title-block cell labels split apart and that
    the bulk partition stays pointwise-consistent with paragraph_at.
    """
    with PdfDocument.open(real_cad_pdf) as doc:
        paras = doc.paragraphs(0)
        for label in ("A", "B"):
            matches = [p for p in paras if p.text == label]
            assert len(matches) == 2  # one per end of its section line
            for p in matches:
                assert (p.bbox[2] - p.bbox[0]) < 20.0  # tight around the glyph
        assert not any(p.text in ("A\nA", "B\nB") for p in paras)

        # Every title-block cell label stands alone (the rows extract as
        # 4 / 2 / 2 / 3 separate editable areas, per the hands-on review).
        for label in (
            "Dept.",
            "Technical reference",
            "Created by",
            "Approved by",
            "Document type",
            "Document status",
            "Title",
            "DWG No.",
            "Rev.",
            "Date of issue",
            "Sheet",
        ):
            assert any(p.text == label for p in paras), label

        # The six rotated dimension labels extract as ROTATED singletons.
        rotated = [p for p in paras if any(s.rotation == 90 for s in p.spans)]
        assert len(rotated) == 6
        assert all(len(p.spans) == 1 for p in rotated)
        assert {p.text for p in rotated} >= {"30", "34", "14"}

        for para in paras:
            for span in para.spans:
                cx = (span.bbox[0] + span.bbox[2]) / 2
                cy = (span.bbox[1] + span.bbox[3]) / 2
                assert doc.paragraph_at(0, cx, cy) == para


def test_real_quote_table_partition_gate(real_quote_pdf):
    """U1 BLOCKING GATE: the real quote's table must partition by row.

    U5 reveal-all and U6 paragraph-select draw their boxes from this
    partition; a table row collapsing into a neighbour's paragraph would make
    that UX wrong. Asserts the FULL invariant on the real sample (skips when
    ``samples/`` is absent):

    - pointwise consistency: ``paragraph_at`` at every member span's centre
      returns the bulk paragraph (bulk and interactive semantics agree);
    - the item-description body is one 3-line paragraph that does NOT bleed
      into the next row's text, despite their line boxes overlapping ~2.4pt;
    - no paragraph mixes different rows' money values, and every paragraph
      containing the row-1 price contains ONLY that price.

    Known accepted quirk (verified, NOT row collapse): a two-line dict block
    — a column header directly over its first row value (DELIVERY/IN STOCK,
    PART NUMBER/...) — cannot be split by pitch, because a single baseline
    delta IS the block's median. ``paragraph_at`` groups those identically
    today; the partition introduces no new grouping.
    """
    with PdfDocument.open(real_quote_pdf) as doc:
        paras = doc.paragraphs(0)

        for para in paras:
            for span in para.spans:
                cx = (span.bbox[0] + span.bbox[2]) / 2
                cy = (span.bbox[1] + span.bbox[3]) / 2
                assert doc.paragraph_at(0, cx, cy) == para

        desc = next(p for p in paras if "C-Thru" in p.text)
        assert len(desc.text.splitlines()) == 3
        assert desc.pitch == pytest.approx(8.0, abs=0.2)
        assert "International Bank" not in desc.text  # next row stays out
        assert any(p.text.startswith("International Bank") for p in paras)

        prices = [p for p in paras if "1,185.47" in p.text]
        assert prices
        assert all(p.text == "1,185.47" for p in prices)  # row/col isolation
        assert not any("1,185.47" in p.text and "25.00" in p.text for p in paras)


def test_replace_paragraph_roundtrip_preserves_pitch_and_neighbours(tmp_path):
    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        result = doc.replace_paragraph(0, para, "replaced alpha line\nreplaced beta line")
        doc.save(out)

    assert result.inserted and result.used_font == "helv" and result.uniform_style
    text = _page_text(out)
    assert "replaced alpha line" in text
    assert "replaced beta line" in text
    assert "body first" not in text and "body third" not in text
    assert "HEADER row" in text  # neighbours intact
    assert "FOOTER far below" in text

    # Reproduced leading: the two new lines keep the original 8pt pitch, and
    # the first baseline stays pinned to the original first line's.
    spans = _spans_for(out)
    alpha = _find(spans, "replaced alpha line")
    beta = _find(spans, "replaced beta line")
    assert beta.origin[1] - alpha.origin[1] == pytest.approx(8.0, abs=0.35)
    assert alpha.origin[1] == pytest.approx(111, abs=0.75)


def test_replace_paragraph_grows_box_for_extra_lines(tmp_path):
    """More lines than the box holds: it grows downward and flags resized."""
    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "grown.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        result = doc.replace_paragraph(0, para, "\n".join(f"grown line {i}" for i in range(6)))
        doc.save(out)
    assert result.resized
    text = _page_text(out)
    for i in range(6):
        assert f"grown line {i}" in text
    assert "HEADER row" in text  # neighbours still intact


def _dense_rows_fixture(tmp_path):
    """A tight-pitch paragraph with ANOTHER row's text just below it.

    Mirrors the real quote's failure geometry: the description paragraph
    (pitch 8) has the next table row 12pt under its last baseline — close
    enough that a grown line lands on it, far enough (> pitch + 0.7) that
    ``paragraph_at`` correctly excludes it.
    """
    path = tmp_path / "dense.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 111), "body first line of the paragraph", fontsize=8)
    page.insert_text((72, 119), "body second line with more words", fontsize=8)
    page.insert_text((72, 127), "body third and final line", fontsize=8)
    page.insert_text((72, 139), "NEXT ROW must stay readable", fontsize=8)
    doc.save(str(path))
    doc.close()
    return path


def test_paragraph_growth_into_occupied_space_refused(tmp_path):
    """Found on the real quote: adding a line break grew the paragraph by one
    line, and the grown line printed straight OVER the next table row (it sat
    exactly one pitch below). Growth into occupied space must refuse instead,
    before any mutation."""
    path = _dense_rows_fixture(tmp_path)
    out = tmp_path / "refused.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        assert "NEXT ROW" not in para.text  # the row below is NOT part of it
        with pytest.raises(ValueError, match="occupied"):
            doc.replace_paragraph(
                0, para, "body first line of the paragraph\nbroken onto\nmore lines\nthan before"
            )
        doc.save(out)

    text = _page_text(out)  # nothing was mutated: original text fully intact
    assert "body first line of the paragraph" in text
    assert "body third and final line" in text
    assert text.count("NEXT ROW must stay readable") == 1


def test_paragraph_growth_into_blank_space_next_to_row_allowed(tmp_path):
    """Same dense fixture, but an edit that KEEPS the line count commits fine —
    the neighbours' bboxes bleeding into the paragraph strip (tight leading)
    must not trip the collision check."""
    path = _dense_rows_fixture(tmp_path)
    out = tmp_path / "same_lines.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        result = doc.replace_paragraph(0, para, "one\ntwo\nthree")
        doc.save(out)
    assert result.inserted and not result.resized
    text = _page_text(out)
    assert "two" in text and "NEXT ROW must stay readable" in text


def test_paragraph_move_onto_text_still_allowed(tmp_path):
    """A MOVE (offset != 0) is deliberate placement — the collision refusal
    applies only to in-place edits."""
    path = _dense_rows_fixture(tmp_path)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        result = doc.replace_paragraph(0, para, para.text, offset=(0.0, 12.0))
        assert result.inserted  # no raise: lands where the user dragged it


def test_real_quote_line_break_growth_refused(real_quote_pdf, tmp_path):
    """The user's exact report: breaking the hose-clamp line onto a new line
    printed the spilled text over 'International Bank or Wire Fees' one pitch
    below. It must refuse instead, leaving the document untouched."""
    with PdfDocument.open(real_quote_pdf) as doc:
        target = next(s for s in doc.text_spans(0) if "hose clamp" in s.text)
        para = doc.paragraph_at(
            0, (target.bbox[0] + target.bbox[2]) / 2, (target.bbox[1] + target.bbox[3]) / 2
        )
        assert "International Bank" not in para.text
        broken = para.text.replace(" (2),", "\n(2),", 1)
        assert broken != para.text  # the break landed
        with pytest.raises(ValueError, match="occupied"):
            doc.replace_paragraph(0, para, broken)
        # No mutation: the hose-clamp line and the row below are both intact.
        spans = doc.text_spans(0)
        assert any("hose clamp (2)" in s.text for s in spans)
        assert sum("International Bank" in s.text for s in spans) == 1


def _right_aligned_pdf(tmp_path):
    """Right-aligned totals-style lines (equal right edges at x=300).

    MuPDF's block segmentation splits heavily-ragged lines — like the real
    quote, the label pair groups into ONE block and the last line stays its
    own; the tests hit the grouped pair.
    """
    path = tmp_path / "right.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    lines = ["Subtotal ex GST", "Shipping Cost (DHL Intl - 000000000)", "Total inc GST"]
    for i, line in enumerate(lines):
        w = pymupdf.get_text_length(line, fontname="helv", fontsize=10)
        page.insert_text((300 - w, 100 + i * 12), line, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _para_at_span(doc, needle):
    span = next(s for s in doc.text_spans(0) if needle in s.text)
    return doc.paragraph_at(0, (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2)


def test_paragraph_alignment_detected(tmp_path):
    with PdfDocument.open(_right_aligned_pdf(tmp_path)) as doc:
        para = _para_at_span(doc, "Subtotal ex GST")
        assert para is not None and len(para.lines) == 2  # label pair, one block
        assert para.align == "right"
    # The left-aligned fixture stays "left" (do-no-harm default).
    with PdfDocument.open(_paragraph_fixture(tmp_path)) as doc:
        assert doc.paragraph_at(0, 100, 118).align == "left"


def test_replace_right_aligned_paragraph_keeps_right_edge(tmp_path):
    """User report: editing re-justified right-aligned blocks to the left.
    The replacement lines must share the original right edge."""
    path = _right_aligned_pdf(tmp_path)
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(path) as doc:
        para = _para_at_span(doc, "Subtotal ex GST")
        doc.replace_paragraph(0, para, "Subtotal excluding GST\nFreight (UPS - 1234)")
        doc.save(out)

    spans = [s for s in _spans_for(out) if "Total inc" not in s.text]
    assert len(spans) == 2
    for span in spans:
        assert span.bbox[2] == pytest.approx(300.0, abs=2.0)  # right edges preserved
    x0s = sorted(s.bbox[0] for s in spans)
    assert x0s[-1] - x0s[0] > 10  # left edges ragged — genuinely right-aligned


def test_centered_paragraph_detected_and_kept(tmp_path):
    path = tmp_path / "center.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    lines = ["a centred heading", "the second centred line of the same block", "short end"]
    for i, line in enumerate(lines):
        w = pymupdf.get_text_length(line, fontname="helv", fontsize=10)
        page.insert_text((200 - w / 2, 100 + i * 12), line, fontsize=10)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "center_edited.pdf"
    with PdfDocument.open(path) as pdoc:
        spans = pdoc.text_spans(0)
        para = next(  # hit whichever span sits in a multi-line block
            p
            for s in spans
            if (p := pdoc.paragraph_at(0, (s.bbox[0] + s.bbox[2]) / 2, s.origin[1] - 2))
            and len(p.lines) >= 2
        )
        assert para.align == "center"
        replaced = [t for t in para.text.split("\n")]
        pdoc.replace_paragraph(0, para, "\n".join("new " + t[:12] for t in replaced))
        pdoc.save(out)
    for s in _spans_for(out):
        if s.text.startswith("new "):
            assert (s.bbox[0] + s.bbox[2]) / 2 == pytest.approx(200.0, abs=2.5)


# --- explicit justification (the alignment toolbar) --------------------------


def test_replace_paragraph_align_override_right_justifies(tmp_path):
    """The user picks Right for a left-aligned paragraph: the replacement
    lines share a right edge and the left edges go ragged."""
    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "right.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        assert para.align == "left"
        doc.replace_paragraph(0, para, "one short\ntwo rather longer line\nmid line", align="right")
        doc.save(out)
    spans = [
        s
        for s in _spans_for(out)
        if s.text.strip() in ("one short", "two rather longer line", "mid line")
    ]
    assert len(spans) == 3
    right_edges = [s.bbox[2] for s in spans]
    assert max(right_edges) - min(right_edges) < 1.0  # one right edge
    left_edges = [s.bbox[0] for s in spans]
    assert max(left_edges) - min(left_edges) > 10  # genuinely ragged left


def test_replace_paragraph_align_override_center(tmp_path):
    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "center.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        doc.replace_paragraph(
            0, para, "one short\ntwo rather longer line\nmid line", align="center"
        )
        doc.save(out)
    mids = [
        (s.bbox[0] + s.bbox[2]) / 2
        for s in _spans_for(out)
        if s.text.strip() in ("one short", "two rather longer line", "mid line")
    ]
    assert len(mids) == 3
    assert max(mids) - min(mids) < 1.0  # shared midpoint


def test_replace_paragraph_align_none_keeps_detected(tmp_path):
    """The default (None) reproduces the DETECTED justification — a move, a
    merge or a plain edit must never re-justify behind the user's back."""
    path = _right_aligned_pdf(tmp_path)
    out = tmp_path / "kept.pdf"
    with PdfDocument.open(path) as doc:
        para = _para_at_span(doc, "Subtotal ex GST")
        assert para.align == "right"
        doc.replace_paragraph(0, para, "Subtotal excluding GST\nFreight (UPS - 1234)", align=None)
        doc.save(out)
    spans = [s for s in _spans_for(out) if "Subtotal excl" in s.text or "Freight" in s.text]
    assert len(spans) == 2
    for span in spans:
        assert span.bbox[2] == pytest.approx(300.0, abs=2.0)


def test_replace_paragraph_left_override_beats_detected_right(tmp_path):
    """The override wins over detection: a right-aligned block explicitly set
    to Left comes back with equal LEFT edges."""
    path = _right_aligned_pdf(tmp_path)
    out = tmp_path / "flattened.pdf"
    with PdfDocument.open(path) as doc:
        para = _para_at_span(doc, "Subtotal ex GST")
        doc.replace_paragraph(0, para, "Subtotal excluding GST\nFreight (UPS - 1234)", align="left")
        doc.save(out)
    spans = [s for s in _spans_for(out) if "Subtotal excl" in s.text or "Freight" in s.text]
    assert len(spans) == 2
    x0s = [s.bbox[0] for s in spans]
    assert max(x0s) - min(x0s) < 1.0


def test_unknown_align_raises_before_any_mutation(tmp_path):
    path = _paragraph_fixture(tmp_path)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        with pytest.raises(ValueError, match="alignment"):
            doc.replace_paragraph(0, para, "replacement text", align="justified")
        # Nothing redacted, nothing inserted — the check is pre-mutation.
        assert doc.paragraph_at(0, 100, 118).text == para.text
        with pytest.raises(ValueError, match="alignment"):
            doc.insert_text(0, (72, 300), "new text", align="middle")
        assert not [s for s in doc.text_spans(0) if s.text.strip() == "new text"]


def test_insert_runs_justify_lines_against_the_widest(tmp_path):
    """Free-standing new text has no box, so the widest line IS the box:
    right/centre line the shorter lines up on it, left leaves them at the
    click point. The point stays the block's left edge in all three."""
    from pdfcore.textedit import StyledRun, TextStyle

    style = TextStyle(size=10)
    runs = [StyledRun("a much longer first line\nshort\nmiddle one", style)]
    edges = {}
    for align in ("left", "center", "right"):
        out = tmp_path / f"insert_{align}.pdf"
        doc = pymupdf.open()
        doc.new_page()
        pdoc = PdfDocument(doc)
        pdoc.insert_runs(0, (72, 200), runs, align=align)
        pdoc.save(out)
        pdoc.close()
        spans = sorted(_spans_for(out), key=lambda s: s.origin[1])
        assert [s.text.strip() for s in spans] == [
            "a much longer first line",
            "short",
            "middle one",
        ]
        edges[align] = spans

    for span in edges["left"]:  # unchanged behaviour: every line at the point
        assert span.bbox[0] == pytest.approx(72.0, abs=1.0)
    rights = [s.bbox[2] for s in edges["right"]]
    assert max(rights) - min(rights) < 1.0
    assert min(s.bbox[0] for s in edges["right"]) == pytest.approx(72.0, abs=1.0)
    mids = [(s.bbox[0] + s.bbox[2]) / 2 for s in edges["center"]]
    assert max(mids) - min(mids) < 1.0


def test_real_quote_totals_block_right_aligned(real_quote_pdf):
    """The quote's totals labels ('Subtotal ex GST' / 'Shipping Cost…' /
    'Total inc GST') are right-aligned and must extract that way."""
    with PdfDocument.open(real_quote_pdf) as doc:
        target = next(s for s in doc.text_spans(0) if "Subtotal ex GST" in s.text)
        para = doc.paragraph_at(
            0, (target.bbox[0] + target.bbox[2]) / 2, (target.bbox[1] + target.bbox[3]) / 2
        )
        assert para is not None
        if len(para.lines) < 2:
            pytest.skip("totals block grouped as a single line — alignment ambiguous")
        assert para.align == "right"


def _overlap_bystander_pdf(tmp_path):
    """A bystander email line at y=150, and a target line just below whose
    redact band (0.10–0.60×size ABOVE its baseline) reaches up into it."""
    path = tmp_path / "overlap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((150, 150), "sales@example.com", fontname="helv", fontsize=9)
    page.insert_text((150, 156), "EDITME", fontname="helv", fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def test_span_edit_preserves_overlapping_bystander(tmp_path):
    """Editing a span must not erase OTHER text its redact band overlaps —
    apply_redactions removes every glyph in the band, so the bystander glyphs
    are captured and re-drawn (the destructive-move bug's root cause)."""
    path = _overlap_bystander_pdf(tmp_path)
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(path) as doc:
        target = _find(doc.text_spans(0), "EDITME")
        doc.replace_text(0, target, "EDITED")
        doc.save(out)

    assert _text_on_row(out, 150.0, exclude=("EDITED",)) == "sales@example.com"
    assert _find(_spans_for(out), "EDITED")  # the edit itself landed


def test_paragraph_move_preserves_overlapping_bystander(tmp_path):
    """The reported bug: moving inserted text whose start overlaps existing
    text wiped the covered section. The foreign text must survive the move."""
    path = _overlap_bystander_pdf(tmp_path)
    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as doc:
        phone = _find(doc.text_spans(0), "EDITME")
        # A phone-only paragraph (the live block the UI would move); the email
        # is a separate block, i.e. genuinely foreign.
        para = Paragraph(
            page_index=0,
            text="EDITME",
            bbox=phone.bbox,
            first_origin=phone.origin,
            pitch=phone.size * 1.2,
            spans=(phone,),
            font=phone.font,
            base14=phone.base14,
            size=phone.size,
            color=phone.color,
            flags=phone.flags,
            embedded=phone.embedded,
            uniform_style=True,
            lines=((phone,),),
            align="left",
        )
        replace_paragraph_runs(
            doc._doc,
            0,
            para,
            [StyledRun("EDITME", TextStyle(code="helv", size=11))],
            offset=(0.0, 80.0),
        )
        doc.save(out)

    # Email intact at its ORIGINAL row; the moved text is now 80pt lower.
    assert _text_on_row(out, 150.0, exclude=("EDITME",)) == "sales@example.com"
    moved = _find(_spans_for(out), "EDITME")
    assert moved.origin[1] == pytest.approx(236.0, abs=1.5)  # 156 baseline + 80 offset


def test_edit_far_from_neighbours_leaves_them_untouched(tmp_path):
    """Guard: the capture/restore must be a no-op when nothing overlaps — the
    neighbour keeps its original single span (not split/re-drawn)."""
    path = tmp_path / "spaced.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 120), "untouched neighbour line", fontname="helv", fontsize=9)
    page.insert_text((72, 160), "EDITME", fontname="helv", fontsize=9)  # 40pt away
    doc.save(str(path))
    doc.close()
    out = tmp_path / "out.pdf"
    with PdfDocument.open(path) as pdoc:
        pdoc.replace_text(0, _find(pdoc.text_spans(0), "EDITME"), "EDITED")
        pdoc.save(out)
    neighbours = [s for s in _spans_for(out) if s.text.strip() == "untouched neighbour line"]
    assert len(neighbours) == 1  # one span, not split by a spurious restore


def _border_px(path, x, y, zoom=4.0):
    doc = pymupdf.open(str(path))
    try:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return max(pix.pixel(round(x * zoom), round(y * zoom)))
    finally:
        doc.close()


def test_edit_preserves_table_border_under_the_band(tmp_path):
    """User bug: a redaction band crossing a table gridline erased the line
    where it overlapped (the white fill painted over it). Editing text whose
    band sits on a border must leave the border intact."""
    path = tmp_path / "border.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_line(pymupdf.Point(100, 145), pymupdf.Point(400, 145), width=1.0)
    page.insert_text((105, 150), "VALUE", fontname="helv", fontsize=11)  # band ~143-149
    doc.save(str(path))
    doc.close()

    assert _border_px(path, 120, 145) < 120  # border present before
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(path) as pdoc:
        span = _find(pdoc.text_spans(0), "VALUE")
        pdoc.replace_text(0, span, "EDITED")
        pdoc.save(out)

    assert _border_px(out, 120, 145) < 120  # border STILL present under the band
    assert "EDITED" in _page_text(out)


def test_move_box_off_a_gridline_keeps_the_gridline(tmp_path):
    """The reported scenario end to end: a box positioned on a gridline, then
    moved away — the gridline it vacated must survive."""
    path = tmp_path / "grid.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_line(pymupdf.Point(60, 200), pymupdf.Point(360, 200), width=1.0)
    page.insert_text((80, 204), "on the line", fontname="helv", fontsize=11)  # band ~197-203
    doc.save(str(path))
    doc.close()

    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as pdoc:
        para = pdoc.paragraph_at(0, 100, 203)
        pdoc.replace_paragraph(0, para, para.text, offset=(0.0, 150.0))
        pdoc.save(out)
    assert _border_px(out, 150, 200) < 120  # gridline survived the move


def test_move_of_overlapping_box_restores_bystander_exactly(tmp_path):
    """The 'EXISSTING' duplicated-glyph bug (E10): capture uses plain bbox
    intersection but MuPDF's removal predicate is stricter, so a glyph on the
    band's edge SURVIVED the redaction and was re-drawn anyway — doubling it.
    Moving a box inserted over existing text must reconstruct the overlapped
    line EXACTLY (every original glyph present exactly once)."""
    original = "EXISTING existing line of text"
    path = tmp_path / "overlap_move.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), original, fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as pdoc:
        pdoc.insert_text(0, (120.0, 200.0), "NEWBOX")  # 11pt box over the line
        box = _find(pdoc.text_spans(0), "NEWBOX")
        para = Paragraph(  # the box as its own paragraph (what the UI moves)
            page_index=0,
            text="NEWBOX",
            bbox=box.bbox,
            first_origin=box.origin,
            pitch=box.size * 1.2,
            spans=(box,),
            font=box.font,
            base14=box.base14,
            size=box.size,
            color=box.color,
            flags=box.flags,
            embedded=box.embedded,
            uniform_style=True,
            lines=((box,),),
            align="left",
        )
        replace_paragraph_runs(
            pdoc._doc,
            0,
            para,
            [StyledRun("NEWBOX", TextStyle(code="helv", size=11.0))],
            offset=(0.0, 100.0),
        )
        pdoc.save(out)

    row = sorted(
        (s for s in _spans_for(out) if abs(s.origin[1] - 200.0) < 1.0),
        key=lambda s: s.bbox[0],
    )
    assert "".join(s.text for s in row) == original  # no doubles, no losses
    moved = _find(_spans_for(out), "NEWBOX")
    assert moved.origin[1] == pytest.approx(300.0, abs=1.5)  # the box moved


def _box_paragraph(span) -> Paragraph:
    """A single-span box as its own paragraph (what the UI moves)."""
    return Paragraph(
        page_index=0,
        text=span.text,
        bbox=span.bbox,
        first_origin=span.origin,
        pitch=span.size * 1.2,
        spans=(span,),
        font=span.font,
        base14=span.base14,
        size=span.size,
        color=span.color,
        flags=span.flags,
        embedded=span.embedded,
        uniform_style=True,
        lines=((span,),),
        align="left",
    )


def test_overlap_move_rebuilds_bystander_as_one_span(tmp_path):
    """E10.3 (user bug): per-glyph restore kept the page visually exact but
    FRAGMENTED the overlapped line in extraction — the existing text then
    grouped/edited as pieces ("broken up into multiple text boxes"). A
    partially-clipped bystander must come back as ONE clean span."""
    original = "EXISTING existing line of text"
    path = tmp_path / "one_span.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), original, fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as pdoc:
        pdoc.insert_text(0, (120.0, 200.0), "NEWBOX")  # box over the line
        box = _find(pdoc.text_spans(0), "NEWBOX")
        replace_paragraph_runs(
            pdoc._doc,
            0,
            _box_paragraph(box),
            [StyledRun("NEWBOX", TextStyle(code="helv", size=11.0))],
            offset=(0.0, 100.0),
        )
        pdoc.save(out)

    row = [s for s in _spans_for(out) if abs(s.origin[1] - 200.0) < 1.0]
    assert len(row) == 1  # ONE span — not remnant + confetti fragments
    assert row[0].text == original
    assert row[0].origin[0] == pytest.approx(100.0, abs=0.5)


def test_overlap_move_spares_tight_neighbour_lines(tmp_path):
    """The rebuild pass re-redacts the clipped span with its thin baseline
    band — tight-set lines above/below must survive it as single spans."""
    path = tmp_path / "tight.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 192), "line above the target", fontname="helv", fontsize=8)
    page.insert_text((100, 200), "the middle target line", fontname="helv", fontsize=8)
    page.insert_text((100, 208), "line below the target", fontname="helv", fontsize=8)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as pdoc:
        pdoc.insert_text(0, (140.0, 201.0), "BOX")  # overlaps the middle line
        box = _find(pdoc.text_spans(0), "BOX")
        replace_paragraph_runs(
            pdoc._doc,
            0,
            _box_paragraph(box),
            [StyledRun("BOX", TextStyle(code="helv", size=11.0))],
            offset=(0.0, 150.0),
        )
        pdoc.save(out)

    for y, text in ((192.0, "line above the target"), (208.0, "line below the target")):
        spans = [s for s in _spans_for(out) if abs(s.origin[1] - y) < 1.0]
        assert len(spans) == 1 and spans[0].text == text  # untouched, unsplit
    middle = [s for s in _spans_for(out) if abs(s.origin[1] - 200.0) < 1.0]
    assert "".join(s.text for s in sorted(middle, key=lambda s: s.bbox[0])) == (
        "the middle target line"
    )


def test_overlap_move_fully_covered_span_reinserted_whole(tmp_path):
    """A short bystander entirely inside the band comes back as one span."""
    path = tmp_path / "covered.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((150, 200), "tiny", fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as pdoc:
        pdoc.insert_text(0, (130.0, 200.0), "A WIDE COVERING BOX")
        box = _find(pdoc.text_spans(0), "A WIDE COVERING BOX")
        replace_paragraph_runs(
            pdoc._doc,
            0,
            _box_paragraph(box),
            [StyledRun("A WIDE COVERING BOX", TextStyle(code="helv", size=11.0))],
            offset=(0.0, 120.0),
        )
        pdoc.save(out)

    row = [s for s in _spans_for(out) if abs(s.origin[1] - 200.0) < 1.0]
    assert len(row) == 1 and row[0].text == "tiny"
    assert row[0].origin[0] == pytest.approx(150.0, abs=0.5)


def test_overlap_move_rebuilds_rotated_bystander_whole(tmp_path):
    """E10.4 (user challenge: "why can't rotated text be supported the same
    way?"): quarter-turn bystanders use the SAME whole-span rebuild — the
    rotated-insertion primitive round-trips origin and direction. (Before
    this, the per-glyph fallback drew a rotated span's clipped glyphs
    HORIZONTALLY — actively garbled, probe-caught.)"""
    path = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((200, 400), "ROTATED DIMENSION TEXT", fontsize=9, rotate=90)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as pdoc:
        rot = next(s for s in pdoc.text_spans(0) if "ROTATED" in s.text)
        assert rot.rotation == 90
        pdoc.insert_text(0, (185.0, 330.0), "BOX")  # horizontal box over its middle
        box = _find(pdoc.text_spans(0), "BOX")
        replace_paragraph_runs(
            pdoc._doc,
            0,
            _box_paragraph(box),
            [StyledRun("BOX", TextStyle(code="helv", size=11.0))],
            offset=(0.0, 150.0),
        )
        pdoc.save(out)

    survivors = [s for s in _spans_for(out) if "BOX" not in s.text]
    assert len(survivors) == 1  # ONE span — not fragments, not sideways glyphs
    assert survivors[0].text == "ROTATED DIMENSION TEXT"
    assert survivors[0].rotation == 90
    assert survivors[0].origin[0] == pytest.approx(200.0, abs=0.5)
    assert survivors[0].origin[1] == pytest.approx(400.0, abs=0.5)


def test_merge_paragraphs_rebuilds_fragments_into_one(tmp_path):
    """E10.7 (user request): a paragraph a box was moved across can fragment
    into several editable zones (repaired lines land in separate blocks).
    Merging the fragments and committing the union re-lays the lines out as
    one contiguous run — extraction sees ONE paragraph again."""
    path = tmp_path / "frag.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 192), "first line of the paragraph", fontname="helv", fontsize=8)
    page.insert_text((100, 200), "second line in the middle", fontname="helv", fontsize=8)
    page.insert_text((100, 208), "third and final line here", fontname="helv", fontsize=8)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "merged.pdf"
    with PdfDocument.open(path) as pdoc:
        # Fragment it: box over the middle line, then moved away (E10.3 path).
        pdoc.insert_text(0, (140.0, 201.0), "BOX")
        box = _find(pdoc.text_spans(0), "BOX")
        replace_paragraph_runs(
            pdoc._doc,
            0,
            _box_paragraph(box),
            [StyledRun("BOX", TextStyle(code="helv", size=11.0))],
            offset=(0.0, 200.0),
        )
        fragments = [p for p in pdoc.paragraphs(0) if "BOX" not in p.text]
        assert len(fragments) >= 2  # the reported fragmentation
        assert sum(len(p.lines) for p in fragments) == 3

        from pdfcore.textedit import merge_paragraphs

        union = merge_paragraphs(fragments)
        assert union.text.split("\n") == [
            "first line of the paragraph",
            "second line in the middle",
            "third and final line here",
        ]
        pdoc.replace_paragraph(0, union, union.text)  # commit = physical rebuild
        pdoc.save(out)

    with PdfDocument.open(out) as pdoc:
        rebuilt = [p for p in pdoc.paragraphs(0) if "BOX" not in p.text]
        assert len(rebuilt) == 1  # ONE editable box again — survives reopen
        assert rebuilt[0].text.split("\n") == [
            "first line of the paragraph",
            "second line in the middle",
            "third and final line here",
        ]


def test_merge_paragraphs_validations(tmp_path):
    from pdfcore.textedit import merge_paragraphs

    path = tmp_path / "v.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 100), "one", fontsize=9)
    page.insert_text((100, 300), "two", fontsize=9)
    page.insert_text((200, 400), "SIDEWAYS", fontsize=9, rotate=90)
    doc.save(str(path))
    doc.close()

    with PdfDocument.open(path) as pdoc:
        one = pdoc.paragraph_at(0, 105, 98)
        two = pdoc.paragraph_at(0, 105, 298)
        rot = next(p for p in pdoc.paragraphs(0) if any(s.rotation != 0 for s in p.spans))
        with pytest.raises(ValueError, match="at least two"):
            merge_paragraphs([one])
        with pytest.raises(ValueError, match="horizontal"):
            merge_paragraphs([one, rot])
        merged = merge_paragraphs([one, two])  # legal: re-pitches uniformly
        assert merged.text == "one\ntwo"


def test_boundaries_isolate_an_inserted_box_from_a_neighbour(tmp_path):
    """User bug: a same-style box inserted one line below existing text gets
    merged into it by MuPDF, so moving the existing line drags the insert.
    An isolation boundary around the inserted box splits them apart."""
    path = tmp_path / "iso.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), "Existing label value here", fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()

    with PdfDocument.open(path) as pdoc:
        pdoc.insert_text(0, (100.0, 212.0), "+64 21 555 0000")  # same style, one line below
        region = next(s for s in pdoc.text_spans(0) if "+64" in s.text).bbox

        # Without boundaries the two lines merge into one paragraph.
        merged = pdoc.paragraph_at(0, 150.0, 199.5)
        assert len(merged.lines) == 2

        # With the insert isolated, each is its own paragraph, both ways.
        existing = pdoc.paragraph_at(0, 150.0, 199.5, boundaries=[region])
        assert [s.text for s in existing.spans] == ["Existing label value here"]
        inserted = pdoc.paragraph_at(0, 150.0, 211.5, boundaries=[region])
        assert [s.text for s in inserted.spans] == ["+64 21 555 0000"]
        assert len(pdoc.paragraphs(0, boundaries=[region])) == 2


def test_fingerprint_boundary_does_not_absorb_foreign_text(tmp_path):
    """Task 5 Level 1: a box moved so its rect overlaps OTHER text must not
    claim that text. With a content fingerprint, the box owns only lines it
    contains; a foreign line under the rect stays its own paragraph."""
    path = tmp_path / "overlap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), "existing line one", fontname="helv", fontsize=9)
    page.insert_text((100, 212), "existing line two", fontname="helv", fontsize=9)
    page.insert_text((100, 260), "moved label", fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()

    with PdfDocument.open(path) as pdoc:
        # A box whose rect overlaps the existing paragraph but whose CONTENT is
        # "moved label". Legacy geometry would absorb the existing lines; the
        # fingerprint keeps them out.
        big_rect = (95.0, 195.0, 260.0, 270.0)
        geo = (big_rect, "moved label")
        legacy = pdoc.paragraphs(0, boundaries=[big_rect])  # bare rect = geometry only
        fp = pdoc.paragraphs(0, boundaries=[geo])  # (rect, text) = content-aware

        # Geometry alone grabs the two existing lines into the box's region.
        legacy_texts = {p.text for p in legacy}
        assert "existing line one\nexisting line two\nmoved label" in legacy_texts

        # The fingerprint keeps the existing paragraph intact and separate.
        fp_texts = {p.text for p in fp}
        assert "existing line one\nexisting line two" in fp_texts
        assert "moved label" in fp_texts
        # And hit-testing the existing text returns ONLY it, not the label.
        hit = pdoc.paragraph_at(0, 150.0, 205.0, boundaries=[geo])
        assert hit.text == "existing line one\nexisting line two"


def test_two_overlapping_fingerprint_boxes_keep_their_own_lines(tmp_path):
    """Two registered boxes whose rects overlap each keep their own content
    (content match beats the shared geometry)."""
    path = tmp_path / "twobox.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), "alpha content", fontname="helv", fontsize=9)
    page.insert_text((100, 214), "beta content", fontname="helv", fontsize=9)
    doc.save(str(path))
    doc.close()

    with PdfDocument.open(path) as pdoc:
        # Both rects overlap both lines' centres.
        a = ((95.0, 195.0, 200.0, 220.0), "alpha content")
        b = ((95.0, 205.0, 200.0, 222.0), "beta content")
        paras = pdoc.paragraphs(0, boundaries=[a, b])
        texts = {p.text for p in paras}
        assert texts == {"alpha content", "beta content"}


def test_fingerprint_stores_visual_lines_and_matches_them(tmp_path):
    """A box's fingerprint is its VISUAL lines (task 5, whole-line matching):
    each laid-out line is stored and owned exactly, and a foreign line whose
    text merely appears INSIDE a box line is NOT absorbed."""
    path = tmp_path / "wrap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((100, 200), "the quick brown", fontname="helv", fontsize=9)
    page.insert_text((100, 212), "fox jumps over", fontname="helv", fontsize=9)
    page.insert_text((100, 260), "quick", fontname="helv", fontsize=9)  # foreign, substring
    doc.save(str(path))
    doc.close()

    with PdfDocument.open(path) as pdoc:
        # Fingerprint = the box's two VISUAL lines (as stored on insert).
        geo = ((95.0, 195.0, 220.0, 220.0), "the quick brown\nfox jumps over")
        para = pdoc.paragraph_at(0, 150.0, 205.0, boundaries=[geo])
        assert para is not None
        assert len(para.lines) == 2  # both visual lines owned by the one box

        # A foreign "quick" line under a box covering it is NOT absorbed
        # (substring of "the quick brown", but not a whole box line).
        wide = ((95.0, 195.0, 220.0, 265.0), "the quick brown\nfox jumps over")
        texts = {p.text for p in pdoc.paragraphs(0, boundaries=[wide])}
        assert "quick" in texts  # stays its own paragraph


def test_boundaries_keep_a_genuine_multiline_paragraph_together(tmp_path):
    """Guard: a boundary must not split a real multi-line paragraph whose
    lines all sit OUTSIDE it (only lines inside the box are isolated)."""
    path = _paragraph_fixture(tmp_path)  # HEADER + 3 tight body lines + footer
    with PdfDocument.open(path) as pdoc:
        # A boundary somewhere empty (far from the body) changes nothing.
        para = pdoc.paragraph_at(0, 100, 118, boundaries=[(400.0, 400.0, 500.0, 460.0)])
        assert "body first line of the paragraph" in para.text
        assert "body third and final line" in para.text  # still grouped


def test_replace_paragraph_impossible_fit_raises_before_mutation(tmp_path):
    path = _paragraph_fixture(tmp_path)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        with pytest.raises(ValueError, match="does not fit"):
            doc.replace_paragraph(0, para, "\n".join(f"line {i}" for i in range(200)))
        # Pre-flighted: the document was NOT redacted.
        assert "body second line with more words" in doc._doc[0].get_text()


def test_replace_paragraph_mixed_style_flags_not_uniform(tmp_path):
    path = tmp_path / "mixed.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Note: ", fontname="hebo", fontsize=9)
    x = 72 + pymupdf.get_text_length("Note: ", fontname="hebo", fontsize=9)
    page.insert_text((x, 100), "regular tail of the same line", fontsize=9)
    page.insert_text((72, 111), "second line entirely regular text", fontsize=9)
    doc.save(str(path))
    doc.close()

    out = tmp_path / "edited.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 99)
        assert para is not None and not para.uniform_style
        assert para.base14 == "helv"  # dominant by text length is the regular style
        result = doc.replace_paragraph(0, para, "flattened replacement text")
        doc.save(out)
    assert not result.uniform_style  # UI must warn: dominant style used
    text = _page_text(out)
    assert "flattened replacement text" in text
    assert "Note:" not in text


def test_replace_paragraph_on_real_quote_description(real_quote_pdf, tmp_path):
    """Paragraph edit of the real description cell: grabs exactly the 3 body
    lines (not the DESCRIPTION header, not the next row), survives round-trip
    with pitch preserved and everything else intact."""
    out = tmp_path / "edited.pdf"
    with PdfDocument.open(real_quote_pdf) as doc:
        spans = doc.text_spans(0)
        seed = next(s for s in spans if "hose clamp" in s.text)
        cx = (seed.bbox[0] + seed.bbox[2]) / 2
        cy = (seed.bbox[1] + seed.bbox[3]) / 2
        para = doc.paragraph_at(0, cx, cy)
        assert para is not None
        lines = para.text.splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("C-Thru")
        assert "DESCRIPTION" not in para.text  # header excluded (pitch break)
        assert "International Bank" not in para.text  # next row: separate block
        assert para.pitch == pytest.approx(8.0, abs=0.1)

        result = doc.replace_paragraph(
            0, para, "C-Thru Separator Standard Package - 230V, 50Hz.\nRevised package contents."
        )
        doc.save(out)

    assert result.inserted and result.used_font == "helv"
    text = _page_text(out)
    assert "Revised package contents." in text
    assert "magnetic base" not in text  # old body gone
    assert "DESCRIPTION" in text  # header intact
    assert "International Bank or Wire" in text  # next row intact
    assert "1,185.47" in text  # prices untouched


# --- move paragraph (E5.1): replace with offset ------------------------------


def test_move_paragraph_offset_roundtrip(tmp_path):
    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        first_before = next(s for s in para.spans if "body first" in s.text)
        doc.replace_paragraph(0, para, para.text, offset=(30.0, 40.0))
        doc.save(out)

    spans = _spans_for(out)
    first_after = _find(spans, "body first line of the paragraph")
    assert first_after.origin[0] == pytest.approx(first_before.origin[0] + 30.0, abs=0.75)
    assert first_after.origin[1] == pytest.approx(first_before.origin[1] + 40.0, abs=0.75)
    # Pitch preserved at the new position; neighbours untouched.
    third = _find(spans, "body third and final line")
    assert third.origin[1] - first_after.origin[1] == pytest.approx(16.0, abs=0.7)
    assert "HEADER row" in _page_text(out)
    assert "FOOTER far below" in _page_text(out)


def test_move_paragraph_clamped_to_page(tmp_path):
    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "moved.pdf"
    with PdfDocument.open(path) as doc:
        page_w, page_h = doc.page_size(0)
        para = doc.paragraph_at(0, 100, 118)
        doc.replace_paragraph(0, para, para.text, offset=(9999.0, 9999.0))
        doc.save(out)

    spans = _spans_for(out)
    moved = _find(spans, "body third and final line")  # bottom line of the block
    assert moved.bbox[2] <= page_w + 0.5  # clamped inside the page
    assert moved.bbox[3] <= page_h + 0.5


def test_move_real_quote_description_paragraph(real_quote_pdf, tmp_path):
    """Manual-pass bug: moving the description paragraph raised 'does not
    fit' — its longest line re-measures a hair wider under the substituted
    font metrics and spuriously wrapped. The width bump must absorb that."""
    out = tmp_path / "moved.pdf"
    with PdfDocument.open(real_quote_pdf) as doc:
        spans = doc.text_spans(0)
        seed = next(s for s in spans if "hose clamp" in s.text)
        cx = (seed.bbox[0] + seed.bbox[2]) / 2
        cy = (seed.bbox[1] + seed.bbox[3]) / 2
        para = doc.paragraph_at(0, cx, cy)
        result = doc.replace_paragraph(0, para, para.text, offset=(8.0, 120.0))
        doc.save(out)

    assert result.inserted
    text = _page_text(out)
    assert "hose clamp (2)" in text  # the paragraph survived the move whole
    assert "magnetic base" in text
    assert "DESCRIPTION" in text  # header untouched
    moved = next(s for s in _spans_for(out) if "hose clamp (2)" in s.text)
    assert moved.origin[1] == pytest.approx(291.0 + 120.0, abs=12.0)  # moved down


# --- insert new text (E5) ----------------------------------------------------


def test_insert_new_text_roundtrip(quote_pdf, tmp_path):
    out = tmp_path / "inserted.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(0, (200.0, 500.0), "Brand new note")
        doc.save(out)

    span = _find(_spans_for(out), "Brand new note")
    assert span.base14 == "helv"
    assert not span.embedded
    assert span.size == pytest.approx(11.0, abs=0.1)
    assert span.color == 0x000000
    assert span.origin == pytest.approx((200.0, 500.0), abs=0.5)  # baseline point


def test_insert_new_text_multiline(quote_pdf, tmp_path):
    out = tmp_path / "inserted.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(0, (200.0, 500.0), "first note line\nsecond note line")
        doc.save(out)
    spans = _spans_for(out)
    first = _find(spans, "first note line")
    second = _find(spans, "second note line")
    assert second.origin[1] > first.origin[1]  # laid out downward


def test_insert_runs_pitch_override_reproduces_tight_spacing(tmp_path):
    """A COPY of a tight-set paragraph must keep ITS pitch — the 1.2-em
    default would space the copy visibly looser than its original."""
    from pdfcore.textedit import StyledRun, TextStyle

    out = tmp_path / "pitched.pdf"
    doc = pymupdf.open()
    doc.new_page()
    pdoc = PdfDocument(doc)
    runs = [StyledRun("copy line one\ncopy line two", TextStyle(size=8))]
    pdoc.insert_runs(0, (72.0, 200.0), runs, pitch=8.0)
    pdoc.save(out)
    pdoc.close()

    spans = sorted(_spans_for(out), key=lambda s: s.origin[1])
    assert len(spans) == 2
    assert spans[1].origin[1] - spans[0].origin[1] == pytest.approx(8.0, abs=0.1)


def test_insert_runs_rejects_a_non_positive_pitch(tmp_path):
    from pdfcore.textedit import StyledRun, TextStyle

    doc = pymupdf.open()
    doc.new_page()
    with PdfDocument(doc) as pdoc:
        with pytest.raises(ValueError, match="pitch"):
            pdoc.insert_runs(0, (72.0, 200.0), [StyledRun("x\ny", TextStyle())], pitch=0)


def test_insert_new_text_rejects_blank_and_off_page(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        with pytest.raises(ValueError, match="no text"):
            doc.insert_text(0, (200.0, 500.0), "   \n ")
        with pytest.raises(ValueError, match="outside the page"):
            doc.insert_text(0, (200.0, 5000.0), "way below the page")


# --- adjustable wrap width (E5.5) ---------------------------------------------


def test_replace_paragraph_with_width_override_rewraps(tmp_path):
    src = tmp_path / "wide.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "a single very long line of text that fills space", fontsize=10)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "narrow.pdf"
    with PdfDocument.open(src) as pdoc:
        para = pdoc.paragraph_at(0, 100, 98)
        assert para is not None
        result = pdoc.replace_paragraph(0, para, para.text, width=120.0)
        pdoc.save(out)

    assert result.inserted
    spans = [
        s
        for s in _spans_for(out)
        if "line of text" in s.text or "single" in s.text or s.text.strip()
    ]
    lines = [s for s in spans if s.size == pytest.approx(10.0, abs=0.2)]
    assert len(lines) >= 2  # narrower box -> the line wrapped
    for span in lines:
        assert span.bbox[2] <= 72.0 + 120.0 + 6.0  # inside the requested width (+bump)


# --- text styles (E5.3) -------------------------------------------------------


def test_insert_styled_size_color_bold(quote_pdf, tmp_path):
    out = tmp_path / "styled.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(
            0,
            (200.0, 480.0),
            "Styled note",
            style=TextStyle(code="hebo", size=14.0, color=0xFF0000),
        )
        doc.save(out)
    span = _find(_spans_for(out), "Styled note")
    assert span.font == "Helvetica-Bold"
    assert span.size == pytest.approx(14.0, abs=0.1)
    assert span.color == 0xFF0000


def test_insert_underlined_draws_line_under_baseline(quote_pdf, tmp_path):
    out = tmp_path / "underlined.pdf"
    items_before = _drawing_items(quote_pdf.path)
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(
            0,
            (200.0, 480.0),
            "underlined words",
            style=TextStyle(size=11.0, underline=True),
        )
        doc.save(out)
    new_items = _drawing_items(out) - items_before
    lines = [it for it in new_items if it[0] == "l"]
    assert lines, "no underline drawn"
    # The underline sits just below the 480 baseline and starts near x=200.
    (_kind, p1, p2) = lines[0]
    assert p1[2] == pytest.approx(480.9, abs=0.8)  # ("P", x, y)
    assert p1[1] == pytest.approx(200.0, abs=1.5)
    assert p2[1] > p1[1] + 20  # spans the text width


def test_insert_strikethrough_draws_line_through_text(quote_pdf, tmp_path):
    out = tmp_path / "struck.pdf"
    items_before = _drawing_items(quote_pdf.path)
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(
            0,
            (200.0, 480.0),
            "struck words",
            style=TextStyle(size=11.0, strike=True),
        )
        doc.save(out)
    new_items = _drawing_items(out) - items_before
    lines = [it for it in new_items if it[0] == "l"]
    assert lines, "no strikethrough drawn"
    # The strike crosses the glyphs ABOVE the 480 baseline (~0.30 x size).
    (_kind, p1, p2) = lines[0]
    assert p1[2] == pytest.approx(480.0 - 3.3, abs=0.8)  # ("P", x, y)
    assert p1[1] == pytest.approx(200.0, abs=1.5)
    assert p2[1] > p1[1] + 20  # spans the text width


def test_insert_underline_and_strike_draw_two_rules(quote_pdf, tmp_path):
    """Both rules coexist: one below the baseline, one across it."""
    out = tmp_path / "both.pdf"
    items_before = _drawing_items(quote_pdf.path)
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(
            0,
            (200.0, 480.0),
            "both rules",
            style=TextStyle(size=11.0, underline=True, strike=True),
        )
        doc.save(out)
    lines = [it for it in (_drawing_items(out) - items_before) if it[0] == "l"]
    assert len(lines) == 2
    ys = sorted(p1[2] for (_k, p1, _p2) in lines)
    assert ys[0] < 480.0 < ys[1]  # strike above the baseline, underline below


def test_insert_superscript_and_subscript(quote_pdf, tmp_path):
    out = tmp_path / "scripts.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(0, (200.0, 480.0), "sup", style=TextStyle(size=10.0, script=SCRIPT_SUPER))
        doc.insert_text(0, (240.0, 480.0), "sub", style=TextStyle(size=10.0, script=SCRIPT_SUB))
        doc.save(out)
    spans = _spans_for(out)
    sup = _find(spans, "sup")
    sub = _find(spans, "sub")
    assert sup.size == pytest.approx(5.8, abs=0.15)  # 0.58 x base size
    assert sub.size == pytest.approx(5.8, abs=0.15)
    assert sup.origin[1] == pytest.approx(480.0 - 3.5, abs=0.3)  # raised
    assert sub.origin[1] == pytest.approx(480.0 + 1.5, abs=0.3)  # dropped


def test_insert_embedded_system_font(quote_pdf, tmp_path):
    arial = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    if not arial.exists():
        pytest.skip("arial.ttf not available")
    out = tmp_path / "sysfont.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(
            0,
            (200.0, 480.0),
            "System font text",
            style=TextStyle(fontfile=str(arial), size=12.0),
        )
        doc.save(out)
    span = next(
        s for s in _spans_for(out) if s.text.replace("\xa0", " ").strip() == "System font text"
    )
    assert span.embedded  # deliberate choice -> embedded subset
    reopened = pymupdf.open(str(out))
    try:
        assert any(f[1] != "n/a" for f in reopened.get_page_fonts(0))
    finally:
        reopened.close()


def test_replace_span_with_style_override(quote_pdf, tmp_path):
    out = tmp_path / "styled_replace.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        result = doc.replace_text(
            0, span, "$2.00", style=TextStyle(code="helv", size=12.0, color=0x0000FF)
        )
        doc.save(out)
    assert result.exact_font  # explicit user style is honoured as chosen
    new_span = _find(_spans_for(out), "$2.00")
    assert new_span.size == pytest.approx(12.0, abs=0.1)
    assert new_span.color == 0x0000FF


def test_replace_paragraph_with_style_override(tmp_path):
    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "styled_para.pdf"
    items_before = _drawing_items(path)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        doc.replace_paragraph(
            0,
            para,
            "styled alpha\nstyled beta",
            style=TextStyle(size=10.0, color=0x008000, underline=True),
        )
        doc.save(out)
    spans = _spans_for(out)
    alpha = _find(spans, "styled alpha")
    beta = _find(spans, "styled beta")
    assert alpha.size == pytest.approx(10.0, abs=0.1)
    assert alpha.color == 0x008000
    # Pitch scaled with the size change: 8pt pitch x (10/8) = 10.
    assert beta.origin[1] - alpha.origin[1] == pytest.approx(10.0, abs=0.5)
    underlines = [it for it in (_drawing_items(out) - items_before) if it[0] == "l"]
    assert len(underlines) >= 2  # one per line


def test_underline_repeated_identical_lines_each_get_one(quote_pdf, tmp_path):
    """Review finding: duplicate line text double-underlined line one and
    skipped line two (non-strict advance guard)."""
    out = tmp_path / "dup.pdf"
    items_before = _drawing_items(quote_pdf.path)
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_text(
            0, (200.0, 480.0), "Hello\nHello", style=TextStyle(size=11.0, underline=True)
        )
        doc.save(out)
    lines = [it for it in (_drawing_items(out) - items_before) if it[0] == "l"]
    ys = sorted(round(it[1][2], 1) for it in lines)
    assert len(ys) == 2
    assert ys[1] - ys[0] > 5.0  # two DISTINCT underlines, one per line


def test_paragraph_underline_spares_neighbour_line_below(tmp_path):
    """Underlines land on exactly the inserted paragraph lines — never on a
    neighbour outside the paragraph.

    NOTE (E9): rewritten — the old fixture's paragraph only ever grouped ONE
    line (median-pitch rule), and the second expected underline was collateral
    on the untouched original line below, drawn by the old extraction-matching
    underliner. Layout-based underlines expose that; this fixture makes the
    paragraph genuinely multi-line (three 10pt-pitch lines; median = 10).
    """
    src = tmp_path / "n.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "para line one", fontsize=10)
    page.insert_text((72, 110), "para line two", fontsize=10)
    page.insert_text((72, 120), "para line three", fontsize=10)
    page.insert_text((72, 135), "NEIGHBOUR below the paragraph", fontsize=12)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "u.pdf"
    items_before = _drawing_items(src)
    with PdfDocument.open(src) as pdoc:
        para = pdoc.paragraph_at(0, 100, 99)
        assert "NEIGHBOUR" not in para.text
        assert len(para.text.splitlines()) == 3
        pdoc.replace_paragraph(0, para, para.text, style=TextStyle(size=10.0, underline=True))
        pdoc.save(out)
    lines = [it for it in (_drawing_items(out) - items_before) if it[0] == "l"]
    assert len(lines) == 3  # one per paragraph line, none on the neighbour
    for it in lines:
        assert it[1][2] < 128.0  # all underlines above the neighbour's line


# --- rich runs: selection-level styling (E9) ---------------------------------


def test_span_runs_two_words_one_bold(quote_pdf, tmp_path):
    """'make just these two words bold': runs land as separate spans with the
    right styles, in order, on one baseline."""
    from pdfcore.textedit import StyledRun

    out = tmp_path / "runs.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        result = doc.replace_text_runs(
            0,
            span,
            [
                StyledRun("total ", TextStyle(code="helv", size=9.0)),
                StyledRun("due", TextStyle(code="hebo", size=9.0)),
            ],
        )
        doc.save(out)

    assert result.inserted
    spans = _spans_for(out)
    total = _find(spans, "total")
    due = _find(spans, "due")
    assert total.font == "Helvetica"
    assert due.font == "Helvetica-Bold"
    assert due.origin[1] == pytest.approx(total.origin[1], abs=0.2)  # same baseline
    assert due.bbox[0] >= total.bbox[2] - 1.0  # 'due' follows 'total'


def test_span_runs_inline_superscript(quote_pdf, tmp_path):
    """A single trailing character as superscript — smaller and raised."""
    from pdfcore.textedit import SCRIPT_SUPER, StyledRun

    out = tmp_path / "sup.pdf"
    base = TextStyle(code="helv", size=10.0)
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        doc.replace_text_runs(
            0,
            span,
            [
                StyledRun("E=mc", base),
                StyledRun("xy", TextStyle(code="helv", size=10.0, script=SCRIPT_SUPER)),
            ],
        )
        doc.save(out)

    spans = _spans_for(out)
    body = _find(spans, "E=mc")
    sup = _find(spans, "xy")  # unique text: the fixture has a standalone "2"
    assert sup.size == pytest.approx(5.8, abs=0.15)  # 0.58 x base
    assert sup.origin[1] == pytest.approx(body.origin[1] - 3.5, abs=0.3)  # raised
    assert sup.bbox[0] >= body.bbox[2] - 1.0


def test_paragraph_runs_preserve_mixed_styles_and_wrap(tmp_path):
    """A bold word mid-text survives as its own span; wrapping happens at word
    boundaries with the bold word kept whole."""
    from pdfcore.textedit import StyledRun

    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "rich_para.pdf"
    base = TextStyle(code="helv", size=8.0)
    bold = TextStyle(code="hebo", size=8.0)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        result = doc.replace_paragraph_runs(
            0,
            para,
            [
                StyledRun("plain words then ", base),
                StyledRun("IMPORTANT", bold),
                StyledRun(" and more plain text following after", base),
            ],
        )
        doc.save(out)

    assert result.inserted
    spans = _spans_for(out)
    important = _find(spans, "IMPORTANT")
    assert important.font == "Helvetica-Bold"
    assert any(s.font == "Helvetica" and "plain words" in s.text for s in spans)
    # Wrapped within the paragraph box (bbox width ~120pt at 8pt text).
    para_right = 72.0 + 130.0
    for s in spans:
        if "plain" in s.text or "IMPORTANT" in s.text or "following" in s.text:
            assert s.bbox[2] <= para_right + 8.0


def test_paragraph_runs_underline_only_marked_run(tmp_path):
    """Underline drawn under exactly the underlined run, not the whole line."""
    from pdfcore.textedit import StyledRun

    path = _paragraph_fixture(tmp_path)
    out = tmp_path / "partial_underline.pdf"
    base = TextStyle(code="helv", size=8.0)
    marked = TextStyle(code="helv", size=8.0, underline=True)
    items_before = _drawing_items(path)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
        doc.replace_paragraph_runs(
            0,
            para,
            [StyledRun("before ", base), StyledRun("under", marked), StyledRun(" after", base)],
        )
        doc.save(out)

    new_lines = [it for it in (_drawing_items(out) - items_before) if it[0] == "l"]
    assert len(new_lines) == 1  # one underline for the one underlined run
    (_kind, p1, p2) = new_lines[0]
    # Underline is a DRAWN line, not a font attribute, so extraction merges
    # the same-font runs back into one span — verify the geometry instead:
    # the line is exactly as wide as "under" and starts after "before ".
    merged = next(s for s in _spans_for(out) if "under" in s.text)
    before_w = pymupdf.get_text_length("before ", fontname="helv", fontsize=8.0)
    under_w = pymupdf.get_text_length("under", fontname="helv", fontsize=8.0)
    assert p1[1] == pytest.approx(merged.bbox[0] + before_w, abs=0.6)
    assert p2[1] - p1[1] == pytest.approx(under_w, abs=0.6)


def test_insert_runs_multiline_mixed(quote_pdf, tmp_path):
    from pdfcore.textedit import StyledRun

    out = tmp_path / "insert_runs.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        doc.insert_runs(
            0,
            (200.0, 500.0),
            [
                StyledRun("first line\nsecond ", TextStyle(size=10.0)),
                StyledRun("bold", TextStyle(code="hebo", size=10.0)),
            ],
        )
        doc.save(out)

    spans = _spans_for(out)
    first = _find(spans, "first line")
    bold = _find(spans, "bold")
    assert first.origin[1] == pytest.approx(500.0, abs=0.3)
    assert bold.font == "Helvetica-Bold"
    assert bold.origin[1] == pytest.approx(500.0 + 12.0, abs=0.5)  # 1.2 x 10pt pitch


def test_paragraph_lines_field_groups_spans(tmp_path):
    path = _paragraph_fixture(tmp_path)
    with PdfDocument.open(path) as doc:
        para = doc.paragraph_at(0, 100, 118)
    assert len(para.lines) == 3
    assert [len(line) for line in para.lines] == [1, 1, 1]
    assert para.lines[0][0].text.startswith("body first")
    assert tuple(s for line in para.lines for s in line) == para.spans


# --- highlight window selection (E9.1) ----------------------------------------


def test_highlight_region_clips_at_character_level(tmp_path):
    """A window covering only the middle words highlights just those — the
    annotation hugs the covered characters, not the whole line."""
    src = tmp_path / "h.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "highlight the middle words only", fontsize=10)
    doc.save(str(src))
    doc.close()

    prefix_w = pymupdf.get_text_length("highlight ", fontname="helv", fontsize=10)
    covered_w = pymupdf.get_text_length("the middle", fontname="helv", fontsize=10)
    out = tmp_path / "out.pdf"
    with PdfDocument.open(src) as pdoc:
        count = pdoc.highlight_region(0, (72 + prefix_w, 92, 72 + prefix_w + covered_w, 102))
        pdoc.save(out)

    assert count == 1
    reopened = pymupdf.open(str(out))
    try:
        page = reopened[0]
        annots = list(page.annots())
        assert len(annots) == 1
        # The annot RECT gets MuPDF padding — the exact geometry is the quad.
        xs = [p[0] for p in annots[0].vertices]
        # Hugs "the middle": starts after "highlight ", ends before " only".
        assert min(xs) == pytest.approx(72 + prefix_w, abs=2.5)
        assert max(xs) == pytest.approx(72 + prefix_w + covered_w, abs=2.5)
    finally:
        reopened.close()


def test_highlight_region_multiple_lines(tmp_path):
    src = tmp_path / "h.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "first line of text", fontsize=10)
    page.insert_text((72, 112), "second line of text", fontsize=10)
    page.insert_text((72, 124), "third line stays clean", fontsize=10)
    doc.save(str(src))
    doc.close()

    with PdfDocument.open(src) as pdoc:
        count = pdoc.highlight_region(0, (60, 90, 200, 114))  # first two lines
        page = pdoc._doc[0]
        assert count == 2
        assert len(list(page.annots())) == 2
        for annot in page.annots():
            assert annot.rect.y1 < 120  # nothing on the third line


def test_highlight_region_empty_returns_zero(quote_pdf):
    with PdfDocument.open(quote_pdf.path) as doc:
        count = doc.highlight_region(0, (400.0, 500.0, 500.0, 560.0))  # blank area
        page = doc._doc[0]
        assert count == 0
        assert list(page.annots()) == []


# --- highlight (E7) -----------------------------------------------------------


def test_add_highlight_roundtrip(quote_pdf, tmp_path):
    out = tmp_path / "highlighted.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        doc.highlight(0, span)
        doc.save(out)

    reopened = pymupdf.open(str(out))
    try:
        page = reopened[0]  # keep the page alive — annots orphan otherwise
        annots = list(page.annots())
        assert len(annots) == 1
        assert annots[0].type[0] == pymupdf.PDF_ANNOT_HIGHLIGHT
        # The annotation covers the span.
        rect = annots[0].rect
        assert rect.x0 <= span.bbox[0] + 1 and rect.x1 >= span.bbox[2] - 1
        # The text itself is untouched (annotations are non-destructive).
        assert quote_pdf.price in page.get_text()
    finally:
        reopened.close()


# --- highlight colour + selection rects (A1) ----------------------------------


def _one_line_pdf(tmp_path, text="alpha beta gamma"):
    src = tmp_path / "h.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), text, fontsize=10)
    doc.save(str(src))
    doc.close()
    return src


def test_highlight_rects_roundtrip(tmp_path):
    src = _one_line_pdf(tmp_path)
    rects = [(72, 92, 140, 102), (72, 104, 140, 114)]
    out = tmp_path / "out.pdf"
    with PdfDocument.open(src) as pdoc:
        count = pdoc.highlight_rects(0, rects)
        pdoc.save(out)

    assert count == 2
    reopened = pymupdf.open(str(out))
    try:
        page = reopened[0]  # keep the page alive — annots orphan otherwise
        annots = list(page.annots())
        assert len(annots) == 2
        assert all(a.type[0] == pymupdf.PDF_ANNOT_HIGHLIGHT for a in annots)
        assert "alpha" in page.get_text()  # non-destructive
    finally:
        reopened.close()


def test_highlight_rects_color_roundtrip(tmp_path):
    src = _one_line_pdf(tmp_path, "colour me")
    pink = (1.0, 0.25, 0.5)
    out = tmp_path / "out.pdf"
    with PdfDocument.open(src) as pdoc:
        pdoc.highlight_rects(0, [(72, 92, 140, 102)], color=pink)
        pdoc.save(out)

    reopened = pymupdf.open(str(out))
    try:
        annot = next(reopened[0].annots())
        assert annot.colors["stroke"] == pytest.approx(pink, abs=0.02)
    finally:
        reopened.close()


def test_highlight_rects_default_is_yellow(tmp_path):
    src = _one_line_pdf(tmp_path, "default yellow")
    with PdfDocument.open(src) as pdoc:
        pdoc.highlight_rects(0, [(72, 92, 160, 102)])
        annot = next(pdoc._doc[0].annots())
        assert annot.colors["stroke"] == pytest.approx((1.0, 1.0, 0.0), abs=0.05)


def test_highlight_region_color(tmp_path):
    src = _one_line_pdf(tmp_path, "region colour test")
    blue = (0.25, 0.77, 1.0)
    with PdfDocument.open(src) as pdoc:
        count = pdoc.highlight_region(0, (60, 92, 300, 104), color=blue)
        annot = next(pdoc._doc[0].annots())
        assert count >= 1
        assert annot.colors["stroke"] == pytest.approx(blue, abs=0.02)


def test_highlight_span_color_roundtrip(quote_pdf, tmp_path):
    green = (0.46, 1.0, 0.01)
    out = tmp_path / "out.pdf"
    with PdfDocument.open(quote_pdf.path) as doc:
        span = _find(doc.text_spans(0), quote_pdf.price)
        doc.highlight(0, span, color=green)
        doc.save(out)

    reopened = pymupdf.open(str(out))
    try:
        annot = next(reopened[0].annots())
        assert annot.colors["stroke"] == pytest.approx(green, abs=0.02)
    finally:
        reopened.close()


# --- extraction: real quote sample (local-only, skips when absent) ---------


def test_real_quote_maps_to_base14(real_quote_pdf):
    spans = _spans_for(real_quote_pdf)

    prices = [s for s in spans if s.text.strip() == "1,185.47"]
    assert prices
    for span in prices:
        assert span.font == "Helvetica"
        assert span.base14 == "helv"
        assert not span.embedded
        assert span.size == pytest.approx(8.0, abs=0.1)
        assert span.color == 0x000000

    date_label = _find(spans, "Date:")
    assert date_label.font == "Helvetica-Bold"
    assert date_label.base14 == "hebo"
    assert date_label.flags & FLAG_BOLD
    assert date_label.size == pytest.approx(9.0, abs=0.1)
    assert date_label.color == 0x000000


def test_real_quote_values_are_single_spans(real_quote_pdf):
    """E4 gate: EVERY occurrence of an editable value is one whole span.

    get_text("dict") may split a visual run into fragments (font/size/colour
    changes); span_at() returns one span, so a fragmented occurrence would
    strand its siblings on edit — even if another occurrence of the same value
    stays whole. Per line, occurrences of the value must equal spans that ARE
    exactly the value. If this test starts failing, STOP before E4 —
    click-to-edit needs a multi-span answer first (per the approved plan).
    """
    doc = pymupdf.open(str(real_quote_pdf))
    try:
        page_dict = doc[0].get_text("dict")
    finally:
        doc.close()

    for value, min_occurrences in [("1,185.47", 3), ("2/7/2026", 1)]:
        occurrences = 0
        whole_spans = 0
        for block in page_dict["blocks"]:
            for line in block.get("lines", ()):
                occurrences += "".join(s["text"] for s in line["spans"]).count(value)
                whole_spans += sum(1 for s in line["spans"] if s["text"].strip() == value)
        assert occurrences >= min_occurrences
        assert whole_spans == occurrences, (
            f"{value!r}: {occurrences} occurrence(s) but only {whole_spans} whole span(s) "
            "— a fragmented occurrence would break single-span click-to-edit"
        )


# --- underline-stroke cleanup (2026-07-18 user report) -----------------------


def _line_strokes(doc: PdfDocument, page_index: int = 0):
    """Single-segment line paths on the page (the shape _draw_underline makes)."""
    out = []
    for path in doc._doc[page_index].get_drawings():
        items = path.get("items", ())
        if len(items) == 1 and items[0][0] == "l":
            out.append((items[0][1], items[0][2]))
    return out


def _underlined_insert_pdf(tmp_path, name="ul.pdf", gridline=None):
    path = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 500), "unrelated body text", fontsize=10)
    if gridline is not None:
        page.draw_line(pymupdf.Point(*gridline[0]), pymupdf.Point(*gridline[1]), width=0.8)
    doc.save(str(path))
    doc.close()
    return path


def test_deleting_underlined_text_removes_drawn_underlines(tmp_path):
    """User report: emptying a text box that had underlined text left the
    underline lines behind (they are line-art, which the text redaction
    rightly preserves — the op must clean up its own strokes)."""
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, underline=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("under lined words", style)])
        assert len(_line_strokes(doc)) == 1  # the underline was drawn
        para = doc.paragraph_at(0, 130.0, 197.0)
        assert para is not None and "under lined" in para.text

        doc.replace_paragraph(0, para, "")  # the delete op (emptied commit)
        assert _line_strokes(doc) == []  # no orphaned underline
        assert all("under" not in s.text for s in doc.text_spans(0))


def test_editing_underlined_text_does_not_accumulate_strokes(tmp_path):
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, underline=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("first version", style)])
        para = doc.paragraph_at(0, 120.0, 197.0)
        # Replacement fits the box on ONE line, so exactly one fresh stroke.
        doc.replace_paragraph_runs(0, para, [StyledRun("second one", style)])
        assert len(_line_strokes(doc)) == 1
        (p1, p2) = _line_strokes(doc)[0]
        new_width = abs(p2.x - p1.x)
        expected = pymupdf.get_text_length("second one", fontname="helv", fontsize=11.0)
        assert new_width == pytest.approx(expected, abs=2.0)


def test_span_delete_cleans_its_underline(tmp_path):
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, underline=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("single line", style)])
        span = next(s for s in doc.text_spans(0) if "single" in s.text)
        doc.replace_text_runs(0, span, [])
        assert _line_strokes(doc) == []


def test_wide_gridline_in_underline_zone_survives_cleanup(tmp_path):
    """A table border crossing just under the text must NOT be eaten by the
    underline cleanup — it is wider than the member span, so it is never a
    candidate (the do-no-harm rule that keeps table borders safe)."""
    # 11pt underline sits ~0.9pt below the 200 baseline; the gridline at
    # y=201.5 is inside the cleanup's search zone but spans the page.
    src = _underlined_insert_pdf(tmp_path, gridline=((50.0, 201.5), (500.0, 201.5)))
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, underline=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("over the line", style)])
        assert len(_line_strokes(doc)) == 2  # gridline + underline
        para = doc.paragraph_at(0, 120.0, 197.0)
        doc.replace_paragraph(0, para, "")
        remaining = _line_strokes(doc)
        assert len(remaining) == 1  # the gridline survives, the underline died
        (p1, p2) = remaining[0]
        assert abs(p2.x - p1.x) > 400  # and it IS the gridline


def test_deleting_struck_text_removes_drawn_strike(tmp_path):
    """Strikethrough is drawn line-art (like underline) that the text redaction
    preserves — the edit/delete op must clean up its own strike strokes too."""
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, strike=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("struck out words", style)])
        assert len(_line_strokes(doc)) == 1  # the strike was drawn
        para = doc.paragraph_at(0, 130.0, 197.0)
        assert para is not None and "struck out" in para.text

        doc.replace_paragraph(0, para, "")  # the delete op (emptied commit)
        assert _line_strokes(doc) == []  # no orphaned strike
        assert all("struck" not in s.text for s in doc.text_spans(0))


def test_wide_gridline_in_strike_zone_survives_cleanup(tmp_path):
    """A table border crossing THROUGH the text (at the strike height) must NOT
    be eaten by the strike cleanup — it is wider than the member span, so it is
    never a candidate (the same do-no-harm rule that protects underlines)."""
    # 11pt strike sits ~3.3pt above the 200 baseline; the gridline at y=196.7
    # is inside the cleanup's strike zone but spans the page.
    src = _underlined_insert_pdf(tmp_path, gridline=((50.0, 196.7), (500.0, 196.7)))
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, strike=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("over the line", style)])
        assert len(_line_strokes(doc)) == 2  # gridline + strike
        para = doc.paragraph_at(0, 120.0, 197.0)
        doc.replace_paragraph(0, para, "")
        remaining = _line_strokes(doc)
        assert len(remaining) == 1  # the gridline survives, the strike died
        (p1, p2) = remaining[0]
        assert abs(p2.x - p1.x) > 400  # and it IS the gridline


# --- rule detection from drawn strokes (2026-07-20 re-edit fix) --------------


def test_extract_detects_inserted_underline(tmp_path):
    """A drawn underline reads back as TextSpan.underline, so re-editing the
    text can show and keep the rule (user report: it vanished on re-edit)."""
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, underline=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("underlined words", style)])
        span = next(s for s in doc.text_spans(0) if "underlined" in s.text)
        assert span.underline
        assert not span.strike


def test_extract_detects_inserted_strike(tmp_path):
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, strike=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("struck words", style)])
        span = next(s for s in doc.text_spans(0) if "struck" in s.text)
        assert span.strike
        assert not span.underline


def test_extract_ignores_wide_gridline_as_rule(tmp_path):
    """A table border wider than the text must NOT read as an underline — the
    same do-no-harm width filter the stroke cleanup uses."""
    # The gridline sits in the underline zone (~0.9pt below the 200 baseline)
    # but spans the whole page, so it is wider than the member span.
    src = _underlined_insert_pdf(tmp_path, gridline=((50.0, 200.9), (500.0, 200.9)))
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0)  # plain — no drawn rule
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("plain words", style)])
        span = next(s for s in doc.text_spans(0) if "plain" in s.text)
        assert not span.underline
        assert not span.strike


def test_paragraph_spans_carry_detected_rule(tmp_path):
    """paragraph_at builds spans the SAME way, so a re-edited paragraph's
    members carry the detected rule (the editor's prefill path)."""
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, underline=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("underlined para", style)])
        para = doc.paragraph_at(0, 130.0, 197.0)
        assert para is not None
        assert all(s.underline for s in para.spans if s.text.strip())


def test_detected_underline_survives_a_real_reedit(tmp_path):
    """Round-trip: an underline detected on open, carried into replacement
    runs, is redrawn on commit — the fix for the vanish-on-edit report."""
    src = _underlined_insert_pdf(tmp_path)
    with PdfDocument.open(src) as doc:
        style = TextStyle(code="helv", size=11.0, underline=True)
        doc.insert_runs(0, (100.0, 200.0), [StyledRun("first ruled line", style)])
        para = doc.paragraph_at(0, 130.0, 197.0)
        # Re-insert carrying the DETECTED rule (what the UI prefill does).
        assert para.spans[0].underline
        reused = TextStyle(code="helv", size=11.0, underline=para.spans[0].underline)
        doc.replace_paragraph_runs(0, para, [StyledRun("kept", reused)])  # short: one line
        assert len(_line_strokes(doc)) == 1  # rule kept, not doubled or dropped
        span = next(s for s in doc.text_spans(0) if "kept" in s.text)
        assert span.underline


def _partial_strike_pdf(tmp_path, name="partial.pdf"):
    """A line struck on the outer words but NOT the middle word 'SKIP'."""
    blank = _underlined_insert_pdf(tmp_path, name="blank_" + name)
    out = tmp_path / name
    struck = TextStyle(code="helv", size=11.0, strike=True)
    plain = TextStyle(code="helv", size=11.0)
    with PdfDocument.open(blank) as doc:
        doc.insert_runs(
            0,
            (100.0, 200.0),
            [StyledRun("keep ", struck), StyledRun("SKIP", plain), StyledRun(" keep", struck)],
        )
        doc.save(out)
    return out


def test_partial_strike_splits_into_rule_segments(tmp_path):
    """A line struck on only some words re-extracts with the UNruled word in
    its own segment — so re-editing shows exactly those words ruled, not the
    whole line (user report: a partial strike re-applied across everything)."""
    with PdfDocument.open(_partial_strike_pdf(tmp_path)) as doc:
        span = next(s for s in doc.text_spans(0) if "SKIP" in s.text)
        assert span.strike  # coarse: SOME of it is struck
        assert "".join(t for t, _u, _s in span.rule_segments) == span.text  # exact
        struck_text = "".join(t for t, _u, s in span.rule_segments if s)
        plain_text = "".join(t for t, _u, s in span.rule_segments if not s)
        assert "SKIP" in plain_text and "SKIP" not in struck_text
        assert "keep" in struck_text


def test_partial_strike_survives_reedit_via_segments(tmp_path):
    """Rebuilding runs from the detected segments (what the UI commit does) and
    re-editing keeps the middle word CLEAR — it is not re-struck."""
    with PdfDocument.open(_partial_strike_pdf(tmp_path)) as doc:
        para = doc.paragraph_at(0, 130.0, 197.0)
        span = para.spans[0]
        runs = [
            StyledRun(t, TextStyle(code="helv", size=11.0, strike=s))
            for t, _u, s in span.rule_segments
        ]
        doc.replace_paragraph_runs(0, para, runs)
        span2 = next(s for s in doc.text_spans(0) if "SKIP" in s.text)
        plain2 = "".join(t for t, _u, s in span2.rule_segments if not s)
        assert "SKIP" in plain2  # the cleared word stayed cleared through the edit
