# List feature v2 — format lists inside one text box (the Acrobat model)

Status: **REDESIGN, replaces the shipped v1 wholesale** (2026-08-18). The v1
plan (L1 recognition, L2 format, L3 insert-list, L4 indent) shipped on
2026-07-26 and was rejected after Scott's hands-on pass of the 0.9.0 build.
Main carries only L1 (recognition), which was never complained about and which
v2 still needs. Everything else on `feat/lists` gets rebuilt to this plan.
The v1 plan's probe facts and review findings that still matter are carried
forward below; the old document is in git history (`dc56e77` and earlier) if
the full narrative is ever needed.

## 1. What was rejected, and what the investigation found

Scott's verdict on the shipped build, all four points blocking release:

1. Two controls for one feature (the "Insert list" toolbar button plus the
   "List style" dropdown) is confusing. Same fault as the two hyperlink
   buttons that got rebuilt into one Ctrl+K command.
2. The Insert-list command should not exist. A list is a format you apply to
   text inside a text box. It is not a placement gesture.
3. The Format-as-list commands appeared to do nothing at all in the real app,
   even though the tests pass.
4. A list must be ONE text box, never one registered box per item.

Point 3 is now reproduced and understood (2026-08-18). The command actually
runs: the body shifts 18 pt right, the registry updates, undo works. What the
user SEES is the problem. The bullet is drawn with base-14 helv, and helv has
no real bullet glyph, so U+2022 lands on the page as a middot (U+00B7), a
speck at reading zoom. On a paragraph that is already a bulleted item, the
command replaces the imported SymbolMT bullet with that speck, which reads as
the bullet vanishing. A numbered item gets "1. " prepended inline with no
hanging indent, which reads as almost nothing. So "zero effect" was accurate
as a description of the visible result, and the root cause is marker
rendering and geometry, and it is in scope for this redesign rather than a
separate bug fix. The repro script lives in git history under
`tests/test_repro_list_bug.py` if it is ever needed again.

Points 2 and 4 are one coherent redesign: a list is several marker-plus-body
blocks living inside a single text box. Point 1 falls out of it, because with
no Insert-list command there is nothing to collide with the format controls.

## 2. The target: what Acrobat actually does

Verified against Adobe's documentation and third-party walkthroughs (sources
at the end of this section). Acrobat's Edit-PDF list behaviour:

- Lists exist INSIDE a text box. Acrobat's own guidance says to think of each
  text box as a separate piece of paper pasted onto the page. There is no
  document-wide list model and no cross-box numbering.
- The Format panel carries a Bulleted-list control and a Numbered-list
  control. Placing the cursor in a paragraph and clicking one converts that
  paragraph to a list item (a cursor with no selection is enough to start a
  new list). Selecting several paragraphs converts them all. Clicking the
  other kind switches the list type. Clicking the HIGHLIGHTED (checked) kind
  with the items selected removes the list formatting and keeps the text, so
  the toggles themselves are the unlist control.
- Enter at the end of an item starts the next item with the next marker, and
  numbers continue automatically. Backspace on a selected row removes it.
- Tab indents the selected item(s) one level; Shift+Tab brings them back.
  Nesting is just deeper indent levels with per-level markers, exactly the
  MS-Office convention. Acrobat refuses to indent further when the text box
  has no room for it.
- Numbering renumbers live within the box while you edit. Once you leave the
  editing session the markers are simply text on the page.

Sources: [Adobe: edit numbered or bulleted lists](https://helpx.adobe.com/ca/acrobat/desktop/edit-documents/edit-text-in-pdfs/edit-lists.html)
(full text supplied by Scott, 2026-08-18, so the steps above are the official
ones verbatim rather than a paraphrase from search results),
[Erin Wright: bulleted and numbered lists in Acrobat](https://erinwrightwriting.com/bulleted-and-numbered-lists-in-adobe-acrobat/),
[Acrobat community on indentation](https://community.adobe.com/t5/acrobat/how-can-i-change-bullet-numbering-indentation/m-p/10550449).

That maps onto this app almost one-to-one. Our equivalent of Acrobat's
text-box editing session is the paragraph editor overlay; our equivalent of
the Format panel is the Text style toolbar; our equivalent of the text box is
a registered box (and box regions already define paragraphs, the E10.7 rule,
so one box holding many list items already edits as one unit with no new
grouping machinery). The v1 design fought the machinery; this one lands on it.

## 3. The model

**A list is a per-block format inside one text box.** While an editor is
open, blocks carry real list structure (Qt's QTextList: live markers, live
renumbering, indent levels). At commit, the engine writes each block's marker
as literal text with a real glyph and a hanging indent, all within the ONE
box, and registers/updates that one box. On the page there is no list data
structure, which keeps v1's Option B decision: no cross-page continuation, no
renumber subsystem at rest, and a sanitizer stripping the registry degrades
gracefully. Renumbering happens wherever Acrobat does it: live, inside the
editing session, regenerated on every commit of that box.

Probed and confirmed (2026-08-18, `scratchpad/probe_qtextlist.py`):

- Inserting a block inside a QTextList block joins the same list and
  renumbers automatically ("1.", "2.", and itemText gives the rendered
  marker). Nested lists are separate QTextLists at deeper indent, and
  rejoining the outer list continues its numbering ("3." after a nested "a."
  run). So the editor gets Word-grade list behaviour from Qt for free; the
  work is seeding, key handling, and commit conversion.
- Arial and Segoe UI both carry real glyphs for • (U+2022), ◦ (U+25E6) and
  ▪ (U+25AA). Neither carries ‣. So the per-level bullet glyphs can be the
  Word-default set (• ◦ ▪) drawn from one embedded Arial subset, and the
  middot problem dies. The existing rule stands: an explicit fontfile embeds
  by deliberate choice, and this is exactly that.

### What the marker looks like on the page

A list block lays out as: marker drawn at `box_left + level × 18 pt`, body
at `marker_x + 18 pt` (or after the marker if it measures wider, e.g.
"viii."), continuation lines wrapping under the body's left edge. Bullets per
level are • ◦ ▪ (cycling if deeper), drawn from the embedded Arial subset at
the block's own size and colour. Numbers per level are the Office defaults
"1." then "a." then "i."; ordinals come from the editor's live numbering at
commit time. Numbered markers ALSO draw in the marker font, not the item's
text font (an LR1 finding): a helv "1." merges with a helv body into one
extracted span, a merged lone item reads as prose under the conservative
detection, and clearing or re-formatting it then does nothing, which is the
"zero effect" failure again. A different-font marker stays its own span, so
the separate-marker geometry always detects it, and Arial digits beside helv
body text are metrically near-identical anyway.

This fixes the v1 numbered-item geometry too: numbered items get the same
hanging indent as bullets instead of an inline "1. " that nothing lines up
under.

### What happens to v1's keep-the-original-bullet-span machinery

It is superseded. v1 kept an imported SymbolMT bullet span through an edit
because redrawing the marker in helv produced the middot. With a real
embedded glyph, redrawing is faithful (SymbolMT's • and Arial's • are the
same character at the same position), so every commit simply redraws markers
from structure. The `keep_span` branch in `replace_paragraph_runs` and the
kept-vs-drawn distinction go away, which also removes the rule that a moved
item degrades to a plain paragraph: moves re-lay the same blocks at the new
position, markers included.

## 4. Engine design (`pdfcore`)

The runs ops grow a per-block dimension. Today `replace_paragraph_runs` and
`insert_new_runs` treat the runs as one flow with one optional `hang`. The
new shape:

```python
@dataclass(frozen=True)
class ListBlock:
    kind: str | None        # "bullet" | "number" | None (plain block)
    level: int = 0          # 0-based indent level
    marker: str = ""        # literal marker text ("•", "3.", "b."), computed
                            # by the caller (the editor's live numbering)
    marker_style: TextStyle | None = None   # bullet glyph font (fontfile
                            # embeds); None = the block's own leading style
```

- `replace_paragraph_runs(..., blocks: Sequence[ListBlock] | None = None)`
  and `insert_new_runs(..., blocks=...)`. `blocks` runs parallel to the hard
  line breaks in the runs: block N formats the Nth logical block. `None`
  keeps today's behaviour exactly (every existing caller and test is
  untouched).
- Layout: a block-aware wrapper around `_layout_runs`. Each laid-out visual
  line carries an x-offset (marker lines at the block's indent, body and
  continuation lines at indent + hang), and `_insert_line` is called with
  that per-line offset. The wrap width per block is the box width minus the
  block's indent + hang. Justification shifts keep working per line against
  the block's own body width.
- The E9.4 growth-collision pre-flight, the fit pre-flight, the redaction +
  bystander/comment/link guards, the fingerprint (`_visual_line_texts` /
  `lines_in_box`), `resized`, and `new_bbox` all operate on the final
  per-line layout, so they need the per-line offsets threaded through but no
  new logic.
- The marker becomes part of the block's first visual line (marker fragment,
  then body fragments starting at the hang offset). One `insert_text` per
  fragment as today. Extraction then sees marker and body on one baseline in
  one block, and with a REAL bullet glyph the extracted text contains the
  real character, so fingerprints are truthful without the v1 lines_in_box
  workaround for the middot. (`lines_in_box` stays: it is still the right
  way to fingerprint what the page will actually re-extract.)
- `set_list_style` and `indent_list_item` survive as thin builders over the
  new path (the right-click menu and tests use them): they derive blocks from
  the paragraph, set every block's kind/level, and commit. The v1 L3 pieces
  (`insert_list_item`, `ListInsertResult`, `slot_is_occupied`,
  `next_item_fits`, `lines_in_box`'s L3-only callers) are deleted with their
  UI in LR4.
- The marker font resolves in the ENGINE by default:
  `lists.marker_fontfile()` probes the standard Windows font paths (Arial,
  then Segoe UI) the same way `pdfcore/ocr.py` probes for tesseract, so
  every caller gets real bullet glyphs without the UI having to know about
  fonts. A caller can still pass its own `marker_style`
  (`TextStyle(fontfile=...)`), and when no font resolves at all the engine
  falls back to the block's base-14 code and the middot rather than
  refusing. Engine tests use the resolved font with the existing
  skip-if-absent pattern.

Detection additions in `pdfcore/lists.py` (pure, no Qt):

- `paragraph_blocks(para) -> list[BlockSpec]` where
  `BlockSpec(kind, level, ordinal, body_lines)`: split a Paragraph's lines
  into logical blocks the editor can seed. A line starting a new block is one
  that begins with a marker (bullet glyph, or an ordinal token) at some
  indent; following lines indented to that block's body x are its
  continuation lines. Level derives from the marker x relative to the box
  left in 18 pt steps. Bullets always count as markers. An ordinal counts as
  a marker only when the block hangs (continuation lines indented) or when
  an adjacent block carries a consecutive ordinal, so a lone paragraph that
  happens to start with "1990 was..." never gets eaten (same conservatism as
  v1's `leading_marker` rules, which this reuses).
- The existing L1 folding (`_folded_page_blocks`) already puts an imported
  separate-span bullet onto the front of its body line, and it stays as is.
  Our own committed markers are inline in the line text, so they need no
  folding at all.

## 5. Editor design (`pdfapp`)

The paragraph editor overlay and the insert editor get list capability; both
are QTextEdit, so this is QTextList wiring:

- **Seeding.** `_begin_paragraph_edit`'s prefill runs `paragraph_blocks` on
  the box's paragraph. List blocks are inserted WITHOUT their literal marker
  text and added to a QTextList of the matching style and indent level
  (consecutive same-kind same-level blocks share one QTextList so numbering
  is continuous; a nested run gets its own list, which is exactly the Qt
  structure the probe confirmed). The user sees real bullets and live
  numbers while editing, and never edits marker text by hand, exactly like
  Acrobat. Document `indentWidth` is set so a level's on-screen indent
  matches the page's 18 pt at the current zoom.
- **Toggles.** Bulleted / Numbered are toolbar toggles (section 6). With an
  editor open they convert the block(s) under the caret or selection to or
  from list blocks (create/join/remove QTextLists). Their checked state
  tracks the caret through the existing `selectionFormatChanged` flow, the
  same way B/I/U do.
- **Keys, matching Acrobat/Word.** In a list block: plain Enter starts the
  next item (Qt already does this; the old L3 list-mode Enter handling is
  deleted). Enter on an EMPTY item removes it from the list and ends the
  list (Word convention, covers Acrobat's backspace-to-end too). Backspace
  at the very start of an item outdents it one level, then removes the list
  format at level 0. Tab at the start of an item (or with a selection)
  indents one level; Shift+Tab outdents. Indenting is refused when the box
  is too narrow to hold the deeper body width, which is Acrobat's refusal,
  surfaced through the existing `editWarning` status path.
- **Commit.** The fragment walk (`_runs_from_pieces`) additionally walks
  blocks: for each block, read its QTextList (kind from the list style,
  level from the format indent, marker text from `itemText` for numbers or
  the per-level glyph for bullets) into a `ListBlock`, and pass the list to
  the engine op. The insert path and the paragraph-commit path share this.
- **No armed mode, no continuation state machine.** Creating a list from
  scratch is: Insert text, click where it goes, press the Bulleted toggle,
  type, Enter, type, Ctrl+Enter. All of v1 L3's `_pending_list`,
  `_continue_list`, `set_list_mode`, per-item undo steps, occupied-slot
  checks and the armed chip are deleted. One editing session commits ONE
  box in ONE undo step, like every other paragraph edit.

## 6. Chrome

All on the Text style toolbar, edit-mode gated like the other content
controls, `_STATE_VERSION` bumped:

- **Bulleted list** and **Numbered list** checkable toggle buttons
  (`format-list-bulleted` / `format-list-numbered` icons). Mutually
  exclusive; checked state reflects the caret's block while an editor is
  open. Clicking the CHECKED toggle removes the list formatting and keeps
  the text, which is Acrobat's own unlist gesture (there is no separate
  "remove" control on the toolbar). With no editor open and a paragraph (or
  multi-selection) selected, clicking applies or clears list formatting as a
  one-shot command through `set_list_style`, so the old right-click path
  keeps working from the toolbar too. This is two buttons for two list KINDS, which is what
  Acrobat and Word both show; the v1 complaint was two controls on two
  different axes (placement vs kind), and that axis is gone.
- **Increase / Decrease indent** buttons (`format-indent-increase` /
  `-decrease`), enabled when the caret or selection is in a list item;
  they drive the same level change as Tab / Shift+Tab.
- The right-click "Format as list" submenu stays (Bulleted / Numbered /
  Remove / Increase indent / Decrease indent), rebuilt over the same
  commands.
- **Deleted:** the Insert-list menu item and toolbar button, the sticky
  "List style" dropdown, the `last_list_kind` setting, the armed "list"
  mode and its chip, and the L3 help-dialog rows. The cheat sheet gains the
  list keys (Enter / Tab / Shift+Tab inside a list).

## 7. Milestones

Each lands green and committed on `feat/lists` before the next starts, per
the repo rules (lint + full pytest; engine changes carry pytest coverage;
round-trip tests for every mutating op).

Status 2026-08-18: LR1 (with LR2's detection folded in — `set_list_style`
needed `paragraph_blocks` on day one), LR3, LR4 and LR4b are committed
green. LR5 is the remaining milestone, and its deciding half is Scott's
hands-on pass.

Two decisions made during LR4b, recorded here because they moved:

- **Ordinal markers now FOLD like bullets do.** v1 skipped them because a
  helv "1." merges into its body span. v2 draws markers in the marker font,
  and MuPDF then puts the Arial "1." and its helv body in separate blocks,
  so without folding a committed numbered item re-extracted as a lone
  marker paragraph plus a plain body (the merge probe caught it). The
  table-row guard: a lone ordinal folds only when it sits within 12 pt of
  its body (`_ORDINAL_FOLD_MAX_GAP`) — a table's number column pads more.
- **The kept-span machinery STAYS.** After LR4b, moves, group offsets,
  duplicates and merges all re-lay lists as blocks, but
  `style_paragraph_selection` (hyperlink styling) still re-lays a paragraph
  without block awareness, and the kept span is what preserves an imported
  bullet through it. Retiring it means making that path block-aware too;
  not worth it for link colour alone.

- **LR1, engine block layout.** `ListBlock`, block-aware layout in
  `replace_paragraph_runs` + `insert_new_runs`, real-glyph markers resolved
  by the engine, hanging numbered markers, `set_list_style` /
  `indent_list_item` rebuilt over it. The v1 kept-span machinery stays
  until LR3: removing it before the UI commits through blocks would regress
  imported-bullet edits mid-branch. Indentation becomes a LEVEL change
  within the box (the Acrobat model) instead of v1's shift of the whole
  box. Tests:
  multi-item round trip in one box, per-level glyphs and ordinals, nested
  indent geometry, growth/fit refusals with blocks, fingerprint truthfulness
  (extracted • is •), bullet-on-bulleted idempotence, embedded-font body
  text with markers, justification within blocks.
- **LR2, detection for seeding.** `paragraph_blocks` in `pdfcore/lists.py`
  with the ordinal conservatism rules; tests on `sample_lists.pdf`,
  `document_with_hyperlink.pdf` (skip-if-absent) and synthetic fixtures,
  including the round trip: commit blocks with LR1, re-detect, get the same
  structure back.
- **LR3, editor integration.** Seeding, toggles, key handling, commit
  conversion, `indentWidth` zoom mapping. Offscreen UI tests drive the
  editor document and commit handlers directly (the established pattern).
  The kept-span machinery stays even after this milestone: box MOVES,
  align/distribute and hyperlink styling still re-lay a paragraph without
  block awareness, and the kept span is what preserves an imported bullet
  there. LR4 makes those paths block-aware (a moved list re-lays as the
  same list), and the kept-span path can only retire after that. A too-deep
  indent is refused at COMMIT time by the engine's width check rather than
  at the Tab keystroke; Acrobat refuses at the keystroke, and moving the
  check earlier is a polish item for the manual pass to judge.
- **LR4, chrome + removal of v1.** Toolbar toggles + indent buttons with
  selection tracking, menu rebuild, delete the L3 machinery end to end
  (UI, engine, settings, help), `_STATE_VERSION` bump, cheat sheet.
  Tests: toggle state tracking, one-shot apply with no editor open, and
  the Markup-mode inertness test for the new controls.
- **LR4b, block-aware box operations.** A moved, group-moved, duplicated or
  merged list box travels AS a list: `paragraph_runs_blocks(para)` derives
  the runs + blocks that re-lay a paragraph exactly as it is (page
  numbering preserved verbatim, so moving a "3." item never renumbers it),
  wired into the four `_runs_from_paragraph` call sites. Registry
  fingerprints refresh on list moves (markers redraw). Explicit-width block
  inserts get the same re-wrap grace as the replace path, or a duplicate
  wrapped a line its original held.
- **LR5, docs + manual pass.** CLAUDE.md current-state rewrite of the Lists
  section, this plan updated to DONE per milestone, and a hands-on pass on
  the real samples (create, convert, nest, renumber, unlist, undo, save,
  reopen, re-edit) before calling the branch releasable.

## 8. Scope decisions

Carried over from v1 unchanged: markers on the PAGE are literal text
(Option B); no cross-page or cross-box list continuation; no auto-renumber
at rest (renumbering is an editing-session behaviour); no ruler, tab stops,
or picture bullets; search/extract see markers as the text they are.

Decided for v2:

- Marker styles are FIXED per level (• ◦ ▪ and "1." "a." "i."), the Office
  defaults. A marker-style chooser is a later refinement and gets designed
  only if asked for; v1's sticky kind-dropdown is gone.
- Numbering restarts per text box, which is Acrobat's model. Formatting a
  multi-selection of separate boxes numbers them in selection order as
  today, but does NOT merge the boxes; a user who wants one list in one box
  uses the existing "Merge text boxes" first.
- An imported item's SymbolMT bullet is redrawn as the embedded Arial bullet
  on the first edit of its box. Accepted trade: identical character, near
  identical rendering, and it buys the removal of the kept-span special
  case.

Hands-on findings ahead of LR5 (2026-08-18, both fixed same-day; full
narrative in `docs/PLAN.md` under the Fable review pass):

1. Merging the four numbered items on sample_lists.pdf page 2 was refused by
   E9.4. The merge pitch took the union median, which for list items is the
   25 pt inter-item GAP, so the re-lay was taller than the boxes it replaced.
   `merge_paragraphs` now pitches at the members' own intra-box advances.
2. A third-level "i., ii." renumbered to ix, x (and a new item to xi): the
   single-letter marker parsed as alpha 9, the 9th letter, and seeded the
   editor's list start there. `lists.ordinal_at_level` re-reads an ambiguous
   letter at its level's ladder rung, so "i." at the roman rung is 1.

Open questions, none blocking LR1:

1. Whether the bulleted/numbered toggles should also live in the right-click
   menu when an editor is OPEN (Acrobat has them in the panel only; we
   currently have no editor-open context menu at all). Default: toolbar
   only.
2. Whether Backspace-at-start outdenting (a Word habit) annoys more than it
   helps in a PDF editor. It is one small handler; drop it if the manual
   pass says so.
3. CLOSED on 2026-08-18: the v1 "ink-wide box" limitation (re-editing a
   created box can hit the E9.4 refusal because its registered rect hugs the
   ink). It was general to all inserted boxes, so it was fixed as its own
   piece of work, `docs/box-width-plan.md`. The wrap width is now measured
   from the page per edit rather than taken from the ink, and a width the
   user drags persists with the box. The registry RECT turned out to be the
   wrong carrier (it is what ownership hit-tests against), which is why that
   plan stores a separate width instead.
