"""List v2 engine tests (LR1): per-block list layout inside ONE box.

Covers the block-aware runs layout (markers with real glyphs, hanging
numbered items, indent levels), the read-side ``paragraph_blocks`` split,
the rebuilt ``set_list_style`` / ``indent_list_item``, and the round trips
rule 10 requires. Glyph-exact assertions skip when no marker font resolves
(non-Windows box) — the layout falls back to helv's middot there.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdfcore.document import PdfDocument
from pdfcore.lists import (
    alpha_ordinal,
    bullet_glyph,
    marker_fontfile,
    marker_text,
    number_marker,
    roman_ordinal,
)
from pdfcore.textedit import (
    ListBlock,
    StyledRun,
    TextStyle,
    fingerprint_lineset,
    list_item_kind,
    paragraph_blocks,
)

HAS_MARKER_FONT = marker_fontfile() is not None
needs_marker_font = pytest.mark.skipif(not HAS_MARKER_FONT, reason="no marker font on this machine")


# --- pure marker generation ---------------------------------------------------


def test_bullet_glyphs_cycle_by_level():
    assert [bullet_glyph(n) for n in range(5)] == ["•", "◦", "▪", "•", "◦"]


def test_alpha_ordinals():
    assert [alpha_ordinal(n) for n in (1, 2, 26, 27, 52)] == ["a", "b", "z", "aa", "az"]


def test_roman_ordinals():
    assert [roman_ordinal(n) for n in (1, 4, 9, 14, 40)] == ["i", "iv", "ix", "xiv", "xl"]


def test_number_markers_cycle_decimal_alpha_roman():
    assert number_marker(0, 3) == "3."
    assert number_marker(1, 3) == "c."
    assert number_marker(2, 3) == "iii."
    assert number_marker(3, 3) == "3."  # cycles at deeper levels


def test_marker_text_dispatch():
    assert marker_text("bullet", 1, 7) == "◦"
    assert marker_text("number", 0, 7) == "7."
    with pytest.raises(ValueError):
        marker_text("dashes", 0, 1)


# --- fixtures -----------------------------------------------------------------


def _para_pdf(tmp_path, lines, x=100.0, y=100.0, pitch=13.2, name="p.pdf"):
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((x, y + i * pitch), line, fontname="helv", fontsize=11)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return path


def _spans(doc):
    return doc.text_spans(0)


# --- set_list_style: real glyphs, hanging markers -----------------------------


@needs_marker_font
def test_bullet_marker_is_a_real_glyph_not_a_middot(tmp_path):
    """The 'zero effect' root cause: helv drew U+2022 as a middot speck.
    With the resolved marker font the page carries a REAL bullet."""
    src = _para_pdf(tmp_path, ["Body text that becomes a bullet item."])
    with PdfDocument.open(src) as doc:
        doc.set_list_style(0, doc.paragraphs(0)[0], "bullet")
        out = tmp_path / "b.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        marker = next(s for s in _spans(doc) if "•" in s.text)
        assert "·" not in marker.text  # the real glyph, not the speck
        body = next(s for s in _spans(doc) if "becomes a bullet" in s.text)
        assert body.bbox[0] == pytest.approx(marker.bbox[0] + 18.0, abs=1.5)


def test_numbered_item_gets_a_hanging_indent(tmp_path):
    """v1 wrote '1. ' inline with no hang; v2 hangs the body like Acrobat."""
    src = _para_pdf(
        tmp_path,
        [
            "A numbered item long enough that its body will",
            "wrap when re-laid at its own width by the op.",
        ],
    )
    with PdfDocument.open(src) as doc:
        para = doc.paragraphs(0)[0]
        left = para.bbox[0]
        doc.set_list_style(0, para, "number", ordinal=3)
        out = tmp_path / "n.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        para = doc.paragraphs(0)[0]
        assert para.text.lstrip().startswith("3.")
        # every body line (the wrap included) hangs at marker + 18
        body_rows = sorted({round(s.origin[1], 1) for s in para.spans})
        assert len(body_rows) >= 2  # it wrapped
        wrapped = [s for s in para.spans if round(s.origin[1], 1) == body_rows[-1]]
        assert min(s.bbox[0] for s in wrapped) == pytest.approx(left + 18.0, abs=1.5)


def test_bullet_on_already_bulleted_item_does_not_stack_markers(tmp_path):
    src = _para_pdf(tmp_path, ["Text that gets bulleted twice."])
    with PdfDocument.open(src) as doc:
        doc.set_list_style(0, doc.paragraphs(0)[0], "bullet")
        doc.set_list_style(0, doc.paragraphs(0)[0], "bullet")
        out = tmp_path / "b.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        para = doc.paragraphs(0)[0]
        marker_chars = [c for c in para.text if c in "•·"]
        assert len(marker_chars) == 1  # one marker, not "• • "
        assert "gets bulleted twice" in para.text


def test_switch_kind_replaces_the_marker(tmp_path):
    src = _para_pdf(tmp_path, ["Item that switches from bullet to number."])
    with PdfDocument.open(src) as doc:
        doc.set_list_style(0, doc.paragraphs(0)[0], "bullet")
        doc.set_list_style(0, doc.paragraphs(0)[0], "number")
        out = tmp_path / "s.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        para = doc.paragraphs(0)[0]
        assert para.text.lstrip().startswith("1.")
        assert "•" not in para.text and "·" not in para.text


def test_clear_list_strips_marker_and_flattens(tmp_path):
    src = _para_pdf(tmp_path, ["Text to bullet and then clear."])
    with PdfDocument.open(src) as doc:
        doc.set_list_style(0, doc.paragraphs(0)[0], "bullet")
        doc.set_list_style(0, doc.paragraphs(0)[0], None)
        out = tmp_path / "c.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        para = doc.paragraphs(0)[0]
        assert para.hang_indent == 0.0
        assert para.text.lstrip()[0] not in "•·◦▪"
        assert "Text to bullet and then clear" in para.text


# --- indent levels (the Acrobat model) ----------------------------------------


@needs_marker_font
def test_indent_cycles_levels_and_glyphs(tmp_path):
    """indent -> level 1 (circle), outdent -> level 0 (disc), outdent at
    level 0 -> plain text (the Word convention)."""
    src = _para_pdf(tmp_path, ["An item stepping through the levels."])
    with PdfDocument.open(src) as doc:
        doc.set_list_style(0, doc.paragraphs(0)[0], "bullet")
        left0 = doc.paragraphs(0)[0].bbox[0]

        doc.indent_list_item(0, doc.paragraphs(0)[0], +18.0)
        indented = doc.paragraphs(0)[0]
        assert indented.bbox[0] == pytest.approx(left0 + 18.0, abs=1.5)
        spec = paragraph_blocks(indented)[0]
        assert (spec.kind, spec.level, spec.marker) == ("bullet", 1, "◦")

        doc.indent_list_item(0, indented, -18.0)
        back = doc.paragraphs(0)[0]
        spec = paragraph_blocks(back)[0]
        assert (spec.kind, spec.level, spec.marker) == ("bullet", 0, "•")

        doc.indent_list_item(0, back, -18.0)  # outdent past level 0 = unlist
        plain = doc.paragraphs(0)[0]
        assert list_item_kind(plain)[0] is None
        assert plain.text.lstrip()[0] not in "•·◦▪"


def test_indent_plain_paragraph_refused(tmp_path):
    with PdfDocument.open(_para_pdf(tmp_path, ["Just prose, not a list."])) as doc:
        with pytest.raises(ValueError):
            doc.indent_list_item(0, doc.paragraphs(0)[0], +18.0)


# --- multi-item lists in ONE box (insert_runs with blocks) --------------------


def _insert_list_box(doc, point=(100.0, 200.0), width=280.0):
    style = TextStyle(size=11)
    runs = [
        StyledRun("Alpha item\n", style),
        StyledRun("Beta item long enough that its body wraps to a second visual line\n", style),
        StyledRun("Gamma nested item", style),
    ]
    blocks = [
        ListBlock("number", 0, "1."),
        ListBlock("number", 0, "2."),
        ListBlock("number", 1, "a."),
    ]
    return doc.insert_runs(0, point, runs, blocks=blocks, width=width)


def test_multi_item_list_lays_out_in_one_box(tmp_path):
    src = _para_pdf(tmp_path, [], name="blank.pdf")
    with PdfDocument.open(src) as doc:
        lines = _insert_list_box(doc)
        out = tmp_path / "list.pdf"
        doc.save(out)
    assert lines[0].startswith("1.") and "Alpha" in lines[0]
    assert lines[1].startswith("2.")
    assert lines[-1].startswith("a.") and "Gamma" in lines[-1]
    with PdfDocument.open(out) as doc:
        spans = _spans(doc)
        marker = next(s for s in spans if s.text.strip() == "1.")
        body = next(s for s in spans if "Alpha" in s.text)
        # The wrap point may shift a word with layout grace — find the
        # CONTINUATION span (item 2's tail on its own line, no marker).
        wrapped = next(s for s in spans if "visual line" in s.text and "Beta" not in s.text)
        nested = next(s for s in spans if "Gamma" in s.text)
        nested_marker = next(s for s in spans if s.text.strip() == "a.")
        # marker at the point, bodies and wraps hang 18 deeper; the nested
        # item's marker steps one level right and its body hangs from there
        assert marker.bbox[0] == pytest.approx(100.0, abs=1.0)
        assert body.bbox[0] == pytest.approx(118.0, abs=1.5)
        assert wrapped.bbox[0] == pytest.approx(118.0, abs=1.5)
        assert nested_marker.bbox[0] == pytest.approx(118.0, abs=1.5)
        assert nested.bbox[0] == pytest.approx(136.0, abs=1.5)


def test_multi_item_box_re_derives_its_structure(tmp_path):
    """The committed list re-reads as the same structure: kinds, levels and
    ordinals survive the round trip (what the editor re-seeds from)."""
    src = _para_pdf(tmp_path, [], name="blank.pdf")
    with PdfDocument.open(src) as doc:
        _insert_list_box(doc)
        out = tmp_path / "list.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        boundaries = ((90.0, 180.0, 390.0, 250.0),)  # one box region
        para = doc.paragraph_at(0, 150, 199, boundaries=boundaries)
        specs = paragraph_blocks(para)
        assert [(s.kind, s.level) for s in specs] == [
            ("number", 0),
            ("number", 0),
            ("number", 1),
        ]
        assert [s.ordinal for s in specs] == [1, 2, 1]
        assert len(specs[1].lines) == 2  # the wrap stayed a continuation line


def test_blocks_must_match_the_hard_break_count(tmp_path):
    src = _para_pdf(tmp_path, [], name="blank.pdf")
    with PdfDocument.open(src) as doc:
        with pytest.raises(ValueError):
            doc.insert_runs(
                0,
                (100.0, 200.0),
                [StyledRun("one\ntwo", TextStyle(size=11))],
                blocks=[ListBlock("bullet", 0, "•")],  # two blocks, one spec
                width=200.0,
            )


def test_no_room_at_deep_indent_refused_pre_mutation(tmp_path):
    src = _para_pdf(tmp_path, [], name="blank.pdf")
    with PdfDocument.open(src) as doc:
        with pytest.raises(ValueError):
            doc.insert_runs(
                0,
                (100.0, 200.0),
                [StyledRun("deep item", TextStyle(size=11))],
                blocks=[ListBlock("bullet", 3, "•")],  # 3 levels eat a 60pt box
                width=60.0,
            )
        assert not _spans(doc)  # nothing landed


# --- renumbering + structure preservation -------------------------------------


def test_format_multiline_box_renumbers_sequentially(tmp_path):
    """Three short lines in one region become 1. 2. 3. — and re-formatting
    with a different start ordinal renumbers, never stacks."""
    src = _para_pdf(
        tmp_path,
        ["First point", "Second point", "Third point"],
        pitch=13.2,
    )
    with PdfDocument.open(src) as doc:
        # one dict block: 3 lines at 13.2pt pitch group as one paragraph
        para = doc.paragraphs(0)[0]
        doc.set_list_style(0, para, "number")
        out = tmp_path / "n.pdf"
        doc.save(out)
    with PdfDocument.open(out) as doc:
        text = doc._doc[0].get_text()
        assert "1." in text and "2." in text and "3." in text
        para = doc.paragraphs(0)[0]
        specs = paragraph_blocks(para)
        assert [s.ordinal for s in specs if s.kind == "number"] == [1, 2, 3]


# --- inline-ordinal conservatism ----------------------------------------------


def test_lone_inline_number_stays_prose(tmp_path):
    """A single paragraph starting '1. ' with no siblings and no hang is
    PROSE — formatting-as-list must not silently eat the '1. '."""
    src = _para_pdf(tmp_path, ["1. This is a heading-style sentence, not a list."])
    with PdfDocument.open(src) as doc:
        specs = paragraph_blocks(doc.paragraphs(0)[0])
    assert [s.kind for s in specs] == [None]


def test_sibling_inline_numbers_read_as_a_list(tmp_path):
    src = _para_pdf(tmp_path, ["1. First thing", "2. Second thing"], pitch=13.2)
    with PdfDocument.open(src) as doc:
        specs = paragraph_blocks(doc.paragraphs(0)[0])
    assert [s.kind for s in specs] == ["number", "number"]
    assert [s.ordinal for s in specs] == [1, 2]


# --- detection on the real Word-export samples (skip when absent) -------------

_SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _sample(name):
    path = _SAMPLES / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    return path


def test_real_sample_bullet_items_read_as_single_blocks():
    """Each imported bullet item is ONE block — including the first item,
    whose last line wraps back to the CONTAINER left (Word's overflow wrap),
    which used to split off as a stray plain block."""
    with PdfDocument.open(_sample("document_with_hyperlink.pdf")) as doc:
        items = [p for p in doc.paragraphs(0) if p.hang_indent > 0]
        assert items
        for para in items:
            specs = paragraph_blocks(para)
            assert [s.kind for s in specs] == ["bullet"]
            assert specs[0].marker_span is not None  # the SymbolMT span, dropped on rebuild


def test_real_sample_numbered_items_read_with_ordinals():
    with PdfDocument.open(_sample("sample_lists.pdf")) as doc:
        ordinals = [
            spec.ordinal
            for n in range(doc.page_count)
            for para in doc.paragraphs(n)
            for spec in paragraph_blocks(para)
            if spec.kind == "number"
        ]
    assert ordinals[:4] == [1, 2, 3, 4]


def test_wrap_back_to_marker_indent_stays_in_the_item(tmp_path):
    """Synthetic twin of the real sample's '…flag to Dar / us.' item: a
    full-width body line whose overflow wraps to the marker indent."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((90, 100), "•", fontsize=11)
    body = "Item body long enough to fill the whole column width and then some more so"
    page.insert_text((108, 100), body, fontsize=11)
    page.insert_text((90, 116), "over.", fontsize=11)  # wrapped to the CONTAINER left
    path = tmp_path / "wrapback.pdf"
    doc.save(str(path))
    doc.close()
    with PdfDocument.open(path) as pdoc:
        para = next(p for p in pdoc.paragraphs(0) if "Item body" in p.text)
        specs = paragraph_blocks(para)
    assert [s.kind for s in specs] == ["bullet"]
    assert len(specs[0].lines) == 2  # the wrap-back line stayed in the item


# --- fingerprints -------------------------------------------------------------


def test_block_mode_fingerprint_matches_extraction(tmp_path):
    """visual_lines must be what the page RE-EXTRACTS through the ownership
    machinery (folded lines, whole-line membership) — the v1 L2 bug was a
    fingerprint the page never matched, leaving the box owning nothing."""
    src = _para_pdf(tmp_path, ["Body that becomes a bulleted item."])
    with PdfDocument.open(src) as doc:
        result = doc.set_list_style(0, doc.paragraphs(0)[0], "bullet")
        para = doc.paragraphs(0)[0]  # the folded re-extraction of the item
        page_set = fingerprint_lineset(para.text)
        stored = fingerprint_lineset("\n".join(result.visual_lines))
        assert stored and stored <= page_set  # every stored line matches the page


# --- ambiguous roman ordinals (user report 2026-08-18) ------------------------


def test_ordinal_at_level_reads_the_rung():
    """ "i." is alpha 9 AND roman 1 — only the level's ladder rung can decide.
    Unambiguous markers keep their parsed value at any rung."""
    from pdfcore.lists import leading_marker, ordinal_at_level

    for text, level, want in (
        ("i.", 2, 1),
        ("v.", 2, 5),
        ("x.", 2, 10),
        ("(iv)", 2, 4),
        ("b.", 2, 2),  # a real letter at the roman rung keeps its value
        ("ii.", 2, 2),  # multi-letter roman is unambiguous
        ("i.", 1, 9),  # at the ALPHA rung "i." really is the 9th letter
        ("c.", 1, 3),
        ("7.", 0, 7),
    ):
        mk = leading_marker(f"{text} body")
        assert ordinal_at_level(mk, level) == want, (text, level)


def test_nested_roman_items_keep_their_ordinals(tmp_path):
    """User report: a third-level "i., ii." re-read as ordinals 9, 10 ("i" is
    also the 9th letter), so an edit renumbered them ix, x and a new item came
    out xi instead of iii. Detected ordinals must be 1, 2 — and regenerating
    the markers from them (the editor's commit loop) with a third item must
    produce iii."""
    src = _para_pdf(tmp_path, [], name="blank.pdf")
    style = TextStyle(size=11)
    runs = [
        StyledRun("First item\n", style),
        StyledRun("2nd item\n", style),
        StyledRun("indented item\n", style),
        StyledRun("further indented\n", style),
        StyledRun("Loving it\n", style),
        StyledRun("Stuff", style),
    ]
    blocks = [
        ListBlock("number", 0, "1."),
        ListBlock("number", 0, "2."),
        ListBlock("number", 1, "a."),
        ListBlock("number", 2, "i."),
        ListBlock("number", 2, "ii."),
        ListBlock("number", 0, "3."),
    ]
    region = (85.0, 85.0, 420.0, 185.0)
    with PdfDocument.open(src) as doc:
        doc.insert_runs(0, (90.0, 100.0), runs, blocks=blocks, width=300.0)
        out = tmp_path / "nested.pdf"
        doc.save(out)

    with PdfDocument.open(out) as doc:
        para = doc.paragraph_at(0, 150.0, 100.0, boundaries=(region,))
        specs = paragraph_blocks(para)
        assert [(s.kind, s.level, s.ordinal) for s in specs] == [
            ("number", 0, 1),
            ("number", 0, 2),
            ("number", 1, 1),
            ("number", 2, 1),  # NOT 9: "i." at the roman rung
            ("number", 2, 2),
            ("number", 0, 3),
        ]

        # The editor commit loop: markers regenerate from the detected
        # ordinals, with a new third roman item spliced in before "Stuff".
        new_runs = [
            StyledRun("First item\n", style),
            StyledRun("2nd item\n", style),
            StyledRun("indented item\n", style),
            StyledRun("further indented\n", style),
            StyledRun("Loving it\n", style),
            StyledRun("what?\n", style),
            StyledRun("Stuff", style),
        ]
        ords = [s.ordinal for s in specs]
        new_blocks = [
            ListBlock("number", 0, marker_text("number", 0, ords[0])),
            ListBlock("number", 0, marker_text("number", 0, ords[1])),
            ListBlock("number", 1, marker_text("number", 1, ords[2])),
            ListBlock("number", 2, marker_text("number", 2, ords[3])),
            ListBlock("number", 2, marker_text("number", 2, ords[4])),
            ListBlock("number", 2, marker_text("number", 2, ords[4] + 1)),
            ListBlock("number", 0, marker_text("number", 0, ords[5])),
        ]
        result = doc.replace_paragraph_runs(0, para, new_runs, blocks=new_blocks)
        joined = "\n".join(result.visual_lines)
        assert "iii." in joined  # the new item
        assert "ix." not in joined and "xi." not in joined  # the bug's output
        out2 = tmp_path / "nested2.pdf"
        doc.save(out2)

    with PdfDocument.open(out2) as doc:  # round-trip: detection stays fixed
        para = doc.paragraph_at(0, 150.0, 100.0, boundaries=(region,))
        deep = [s.ordinal for s in paragraph_blocks(para) if s.level == 2]
        assert deep == [1, 2, 3]


# --- merging list items (user report 2026-08-18, sample_lists.pdf page 2) -----


def test_real_sample_numbered_items_merge_into_one_list(tmp_path):
    """The reported gesture: the four "Key scope questions" items on page 2
    merge into ONE numbered list. The union used to take the 25 pt inter-item
    gap as its pitch, re-lay taller than the boxes it replaced, and E9.4
    refused against "I have not started building" one paragraph below."""
    from pdfcore.textedit import merge_paragraphs, paragraph_runs_blocks

    with PdfDocument.open(_sample("sample_lists.pdf")) as doc:
        items = [p for p in doc.paragraphs(1) if p.text.lstrip()[:2] in ("1.", "2.", "3.", "4.")]
        assert len(items) == 4
        union = merge_paragraphs(items)
        assert union.pitch == pytest.approx(17.0, abs=0.5)  # the ITEMS' pitch, not the gap
        runs, blocks = paragraph_runs_blocks(union)
        result = doc.replace_paragraph_runs(1, union, runs, blocks=blocks)
        assert result.inserted
        out = tmp_path / "merged.pdf"
        doc.save(out)

    with PdfDocument.open(out) as doc:
        paras = doc.paragraphs(1)
        merged = next(p for p in paras if "How far" in p.text)
        for marker in ("1.", "2.", "3.", "4."):
            assert marker in merged.text  # numbering survived the merge
        assert "Nesting in scope" in merged.text
        below = next(p for p in paras if "not started building" in p.text)
        assert below.bbox[1] == pytest.approx(297.7, abs=1.0)  # bystander untouched
