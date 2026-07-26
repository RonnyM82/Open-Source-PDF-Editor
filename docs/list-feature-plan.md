# Design plan — numbered & bulleted list recognition + formatting

Status: **PROPOSAL for review** (not yet built). Requested 2026-07-26 alongside
the embedded-font edit-bug fix. Deliberately scoped in phases so we can stop at
any green boundary.

## 1. Problem

Word/browser exports encode a list item as a marker glyph plus hanging text, but
our paragraph model treats the marker as its OWN one-line paragraph, divorced
from the item text. Grounded in `samples/document_with_hyperlink.pdf` (each list
item is ONE MuPDF dict block):

```
block 6:  l0  x0=90   "•"                     baseline 312.9  (SymbolMT)
          l1  x0=108  "G (Foam Endmill) — …"  baseline 312.9  (Aptos,Bold)  ← SAME baseline as the bullet
          l2  x0=108  "it on JU09 …"          baseline 329.8
```

- The marker sits at the **marker indent** (x0=90) and shares the **first text
  line's baseline** — it is a margin marker, not a row of its own.
- The item text hangs at a deeper **text indent** (x0=108).
- `paragraphs_on_page` today returns `["•"]` (1 line, x0=90) and
  `["G (Foam Endmill) …"]` (3 lines, x0=108) as **two unrelated paragraphs**.

Acrobat groups marker+text as one list item and knows the sequence is a list.
We want the same: recognise lists, and let the user format/create them.

The samples only contain **bulleted** lists (SymbolMT `•`). Numbered lists must
be designed for even though no sample exercises them yet — Word writes the
number as literal marker text (`1.`, `1)`, `a.`, `i.`), same geometry as a
bullet marker.

## 2. What a "list" is (detection contract)

A **list item** is a dict block (or a box-registry region) where:

1. The block's first line is a **lone marker** at marker-indent `x_m`:
   - a bullet glyph — `• ◦ ▪ ‣ · –` and the SymbolMT/Wingdings/Courier bullet
     code points (maintain a small table; extraction already sees the glyph
     text), OR
   - an **ordinal token** matching `^(\(?\d+[.)]?|\(?[a-zA-Z][.)]|[ivxlcdm]+[.)])\s*$`
     (decimal, alpha, roman; with `.` `)` or wrapping parens).
2. The remaining lines (the body) start at a **deeper** text-indent
   `x_t > x_m` (a hanging indent; tolerance ~2 pt), and the first body line
   shares the marker's baseline (±0.5×size).
3. The marker line holds ONLY the marker (+ trailing space).

A **list** is a maximal run of consecutive items with matching `(x_m, x_t)` and a
compatible marker family (all bullets, OR an ordinal sequence — not necessarily
gap-free; Word restarts/ skips happen). Blank-line gaps between items are
allowed. Nesting = a deeper `(x_m, x_t)` pair inside an outer list (Phase L4).

Engine home: a new pure module `pdfcore/lists.py` (no Qt), consuming the spans
`extract_spans` already produces. Detection is a read-only classifier over the
existing geometry — it does not change extraction.

## 3. Phased milestones

Each phase is independently shippable and testable. **L1 is the fix for the
reported grouping complaint**; L2+ are the "create/format" ask.

> **DECISION (2026-07-26): the marker is EDITABLE TEXT, not a managed property
> (scope Q7 → Option B).** Rationale: a numbered list that flows across a page
> break would, under the managed-property model, need a cross-page
> list-identity + continuation + renumber subsystem — which this per-page,
> no-reflow engine deliberately doesn't have. Treating the marker as ordinary
> leading text sidesteps all of it. **Consequences baked into the phases
> below:** a "list" is just *a leading marker + a hanging indent* (a formatting
> style, not a data structure); numbered creation writes `1. 2. 3.` as literal
> text once; **no auto-renumber** (scope Q2 → manual); L4 collapses to
> incremental indent via Tab/Shift+Tab (no drag, no renumber).
>
> **Dependency: L1 needs paragraph editing to REFLOW.** The paragraph editor
> currently bakes a hard break between every wrapped visual line, so a re-layout
> can't re-wrap (this is the same root cause as the "change the font → grows
> into occupied space" bug, fixed separately first). List items are just
> hanging-indent paragraphs, so they inherit that fix.

### L1 — Recognition & clean editing (the grouping fix)

Goal: a list item edits as ONE unit — marker + hanging-indent body — and the
marker rides along as the first token of the item's text (Option B).

- `pdfcore/lists.py`: `ListMarker(kind, text, bbox, ordinal)`,
  `ListItem(marker, body_spans, marker_indent, text_indent, bbox)`,
  `detect_lists(doc, n) -> list[ListItem]` (pure). Detection is READ-ONLY — it
  drives grouping, the toolbar toggle state, and "unlist"; it never owns the
  marker.
- **Two marker encodings to handle** (from the samples): a *bullet* is a
  SEPARATE dict span (SymbolMT `•` at `x_m`) sharing the first body line's
  baseline; a *number* is INLINE — `"1. "` is the leading text of the body span
  itself. Detection normalises both to "a leading marker + hanging body".
- Integrate with the **box/paragraph model**: `paragraph_at` / `paragraphs`
  treat a detected item's marker+body as ONE `Paragraph` with a **hanging
  indent** (first line at `x_m`, wrapped continuation at `x_t`). The marker is
  part of the paragraph's text, so editing shows and can retype it. Reuses the
  existing `_partition_lines` / box-region machinery.
- Editing re-lays the item through `replace_paragraph_runs` with the hanging
  indent; the marker is just text at the front, so nothing special re-draws it.
  This is the actual Acrobat-style grouping the report asked for.

Tests: detection on `document_with_hyperlink.pdf` + `sample_lists.pdf` (bullets,
inline numbers, hanging-indent numbers), round-trip edit keeps the marker +
hanging indent, reflow works after a font change.

### L2 — Format existing paragraphs as a list / unlist

Goal: select paragraphs → "Bulleted list" / "Numbered list" toggle.

- Engine `make_list(doc, n, paragraphs, kind, start=1)`: prepend a marker as
  TEXT to each paragraph (`"•\t"`-style, or literal `"1. " "2. " …` in
  selection order) and set the hanging indent. One undoable command via the runs
  engine.
- Engine `clear_list(doc, n, items)`: strip the leading marker text + flatten
  the indent.
- UI: two toolbar toggles on the Text style toolbar (icons
  `format-list-bulleted` / `format-list-numbered`), enabled on a multi-select or
  an open paragraph editor; checked when the caret's paragraph is already a list
  item. Toggling off unlists.
- **Numbered creation is a one-shot** — the numbers are literal text. Inserting
  or reordering items later does NOT renumber; the user fixes numbers by editing
  the text (Option B).

Tests: format 3 paragraphs → 3 items with correct marker text + indent; unlist
restores; round-trip.

### L3 — Creation & continuation — **DONE (2026-07-26)**

Goal: start a list from scratch; Enter continues it. Shipped as designed; the
current-state reference now lives in CLAUDE.md's "List recognition" section.
What was built, and the decisions taken along the way:

- **Engine `textedit.insert_list_item(doc, n, point, runs, kind, *, ordinal,
  align, pitch, width) -> ListInsertResult`** — lays the item out DIRECTLY with
  the shared primitives (`_layout_runs` / `_insert_line` / `_align_shift`, the
  ones `replace_paragraph_runs` uses): marker at the point, body wrapped and
  hung at `point + hang`, everything validated before the page is touched.
  `ListInsertResult` carries `bbox`, `visual_lines`, `next_point` (the next
  item's baseline, at this item's left edge so markers line up) and
  `next_bbox` (the slot that item would occupy).
- **The kickoff note's two-step was built first and then REJECTED** (see the
  review section below). It composed `insert_new_runs` + `set_list_style`,
  which meant reading the text back off the page between the halves — and that
  round trip carried three separate failures. Direct layout also lets the body
  WRAP, so an ordinary prose-length item no longer has to be refused.
- **`next_point` is one line pitch (1.2 em) below the item's last line.** That
  is normal list spacing and it is BELOW MuPDF's dict-block split threshold
  (~1.50–1.53 × font size, measured), so consecutive items land in ONE block
  and read as one paragraph unless something isolates them. The box registry
  does: each item registers its own box inside the undoable command, and every
  UI read goes through `page_geometry`, which passes those boundaries.
  A 5-item run at 13.2 pt spacing stays 5 clean items.
- **The bullet fingerprint bug (found here, fixed here).** A bullet is drawn as
  its OWN span; MuPDF folds it onto the front of the first body line and helv
  renders U+2022 as U+00B7, so the page reads `"·Item"` while the layout knows
  `"Item"`. A fingerprint built from the laid-out text therefore never matched
  its own line, `_line_region` returned −1, and the box owned nothing — with 3+
  items the 2nd and 3rd were re-laid as continuation lines of the 1st. Fix:
  `textedit.lines_in_box(doc, n, rect, origins)` re-reads the page, restricted
  to the lines actually drawn.
- **`origins` is load-bearing in the other direction.** The first version
  selected by geometry alone; because the item's box is marker-widened, a value
  in the next column sharing a baseline landed inside it, so the box owned that
  foreign line and a later edit physically RELOCATED it into the box. Review
  caught it by A/B-ing against HEAD. The same geometry-only helper had been
  applied to the shipped L2/L4 registry updates as a fix for the fingerprint
  bug there; that was **reverted** — it turned a mild latent defect (a
  formatted bullet box owning nothing) into a corrupting one. See §7.
- **Keys (the continuation contract).** In list mode the paragraph editor reads
  plain **Enter** as commit-this-item-and-start-the-next, **Shift+Enter** as
  the line break within an item (Enter's old job needed a home), **Ctrl+Enter**
  as commit-and-end (its app-wide "apply" meaning is preserved), and **Esc** as
  end-the-list. An empty item ends the list too — nothing to clean up, because
  the marker is just text (Option B). Each item is its own undo step.
- **The marker is NOT seeded into the editor.** The engine draws it, so what the
  user types is exactly the item's body; the status hint names the item
  instead, and deliberately does NOT show a "•" — a base-14 marker draws as a
  middot, so promising the glyph would not match what lands on the page.
  Conversely a marker-shaped token in freshly TYPED text is KEPT: unlike
  `set_list_style` (which re-formats text already on the page) there is nothing
  to re-format here, and stripping silently deleted the "a) " from "a) see
  appendix".
- **The dropdown is a KIND chooser, not a glyph palette** (deviation from §6.1):
  creation writes only `•` / `1.`, and reproducing `◦ ▪ –` faithfully needs an
  EMBEDDED symbol font. Probed both ways: with `TextStyle(fontfile=…)` (Arial,
  Segoe UI Symbol) `• ◦ ▪ – ‣ ●` all write and re-extract as themselves; with
  every base-14 code (helv/tiro/cour/symb/zadb) they collapse to U+00B7. So a
  glyph palette is *possible*, just a bigger choice than L3 needed — see open
  question 2.

**Known limitation (not fixed here).** A created item's registry box is
INK-wide, and `replace_paragraph_runs` wraps to `para.bbox` — so re-editing an
item to ADD a word can wrap it to two lines and hit the E9.4 "the space it
would grow into is already occupied" refusal against the item below. The
workaround is the paragraph editor's existing right-edge/corner drag (which
sets the wrap width). Fixing it properly means changing how an inserted box's
wrap width is derived, which is a separate change touching every inserted text
box, not just lists. It is the sharpest remaining edge on the feature.

### What adversarial review changed (2026-07-26, 4 lenses)

Every one of these was reproduced before it was fixed; the first three are why
the two-step insert-then-restyle shape is gone.

1. **Styling the wrong paragraph.** `paragraph_at` resolves OVERLAPPING lines by
   dict-block order, so an item landing near existing prose returned the
   EXISTING paragraph: `set_list_style` then bulleted it and shoved its body
   18 pt right, leaving the typed item plain. Reproduced on
   `samples/document_with_hyperlink.pdf` by clicking a genuine gap — the
   continuation walked items 2 and 3 onto real content.
2. **Isolation lapsing on non-ASCII.** The boundary fingerprint used the
   laid-out text, which does not round-trip for anything base-14 lacks (a typed
   `•`, an em dash, a curly quote, CJK — all extract as U+00B7), so
   `_line_region` missed, isolation silently lapsed, and the item merged with
   its neighbour.
3. **Not atomic.** The plain text was already on the page when the second half
   raised (e.g. over a rotated line).
4. **Baseline clamping.** `next_point` came from the REQUESTED baseline, but
   `replace_paragraph_runs` clamps near the page top — three items started at
   y=5 stacked two on one baseline. Direct layout refuses instead.
5. **Auto-placing onto occupied space.** The user picks only the FIRST position,
   so `slot_is_occupied` now ends the list rather than typing over content.
6. **`_pending_list` hijacking a plain insert.** Starting "Insert text" mid-list
   inherited the stale (kind, ordinal) and produced the list's next item.
   `_on_insert_point` clears it.
7. **`editorClosed` firing mid-list.** It runs after the commit handler, which
   has already re-opened the editor for the next item — so the style toolbar
   snapped back to its global defaults while the user was still typing, and the
   next item committed with the global alignment instead of the picked one.
8. **Shift+Enter produced U+2028**, a soft break the layout did not split on, so
   a two-line item laid out as one 283 pt line. Fixed at both ends (the editor
   inserts a real block; `_layout_runs` translates U+2028/U+2029) — this was a
   latent bug in the paragraph editor generally, not just in lists.
9. **The continuation editor could walk below the viewport** at any zoom above
   fit-page. `_continue_list` scrolls it into view.

### L4 — Incremental indentation (much reduced under Option B)

Just **Tab / Shift+Tab (and toolbar Increase/Decrease-indent) step the hanging
indent by one unit** (§6.4). No drag, no auto-renumber, no nesting data model —
"nesting" is simply a deeper hanging indent, and because markers are literal
text there is nothing to renumber. This is now a small geometry step, not a
word-processor subsystem.

## 4. Engine API sketch (`pdfcore/lists.py`)

```python
@dataclass(frozen=True)
class ListMarker:
    kind: str                 # "bullet" | "decimal" | "alpha" | "roman"
    text: str                 # the literal marker ("•", "1.", "a)")
    bbox: tuple[float, float, float, float]
    ordinal: int | None       # 1 for "1.", 3 for "c.", None for a bullet

@dataclass(frozen=True)
class ListItem:
    marker: ListMarker
    body_spans: tuple[TextSpan, ...]
    marker_indent: float
    text_indent: float
    bbox: tuple[float, float, float, float]

def detect_lists(doc, n) -> list[ListItem]: ...          # L1
def make_list(doc, n, paragraphs, kind, start=1): ...    # L2 (undoable via UI command)
def clear_list(doc, n, items): ...                       # L2
def insert_list(doc, n, point, items, kind): ...         # L3
```

All mutating ops go through the existing SnapshotCommand/undo funnel and the
`after_command` cache invalidation, like every other content op.

## 5. Integration points (reuse, don't reinvent)

- **Paragraph/box regions** (`_partition_lines`, box registry): a list item is a
  region; the hanging indent is stored the way a box's rect is. This keeps L1
  consistent with the "box regions DEFINE paragraphs" rule.
- **Runs engine** (`replace_paragraph_runs`, `insert_new_runs`): marker + body
  are laid out with the existing greedy-wrap layout; the marker is a run pinned
  at `x_m`, the body wraps at `x_t`.
- **Font handling**: the marker glyph often uses SymbolMT (an embedded symbol
  font) — the embedded-font-reuse path just landed reuses it for faithful
  re-insertion; a numbered marker uses the item's own text font.
- **Geometry cache**: list detection rides the same per-page cache as
  paragraphs/geometry (add to the `after_command` funnel, don't invent a new
  invalidation path).

## 6. UI & interaction design

Grounded in patterns the app already has — nothing here invents a new widget
kind. **Per the §3 decision, the marker is EDITABLE TEXT (Option B)** — the app
does not own it. That simplifies the whole interaction: a list item is just a
paragraph with a leading marker and a hanging indent.

### 6.1 Toolbar

All controls live on the existing **Text style toolbar**, next to the
justification dropdown, and are **edit-mode only**. Bump `_STATE_VERSION`
(main_window.py) so a stale saved layout is dropped. Icons come from
`pdfapp/icons.py` (`mdi6.*`), re-baked on theme change via `_assign_icons`.

- **Bulleted list** toggle (`format-list-bulleted`) and **Numbered list** toggle
  (`format-list-numbered`). Mutually exclusive; **checked** when the caret /
  selection sits in a list of that kind (reflected the same way B/I/U track the
  selection — `selectionFormatChanged` / `styleContextChanged`). Clicking a
  checked toggle **unlists** (`clear_list`).
- **Marker-style dropdown** — built by the SHARED `MainWindow._make_dropdown_
  button` (InstantPopup; the exact flat-button-with-corner-arrow used by the
  alignment control and the highlighter swatch — NOT a split button). The button
  wears the active marker's glyph; its menu lists the kind's options
  (`•  ◦  ▪  –` for bullets; `1.  1)  (1)  a.  i.` for numbered) as an exclusive
  `QActionGroup`. **Sticky** — persisted like `last_text_align` /
  `last_highlight_color` so it starts on the last-used marker.
- **Increase / Decrease indent** buttons (`format-indent-increase` /
  `-decrease`). Distinct MDI glyphs from the `format-align-*` justification set
  (same distinction already drawn for `box_align_*` vs `format-align-*`). Step =
  one indent unit (§6.4). Disabled at level 0 for decrease.

Gating mirrors the other content controls: enabled on `has_page and edit_mode`
with a text selection or an open paragraph editor; a checked/greyed state
reflects the caret's paragraph. Disabled on rotated or embedded-unsupported
spans (same refusal the paragraph editor already makes).

### 6.2 Editing an existing list item

The marker is editable text (Option B), so **the paragraph editor opens over the
whole item, marker included** — the item is one hanging-indent paragraph.
Concretely:

- Double-click (or Ctrl+double-click) an item → `paragraph_at` returns the
  list-aware `Paragraph` (hanging indent: first line at `x_m`, wraps at `x_t`).
  The overlay opens with the marker as the first characters of the text; the
  user can retype or delete it like any text. Reuses the existing overlay.
- The bullet case needs one adaptation: on the page the `•` is a separate span
  at `x_m`; detection stitches it onto the front of the body text so the editor
  shows `• text…`. Committing re-emits it at the hanging indent.
- **Enter stays a line break** within the item; Ctrl+Enter commits. Commit
  re-lays through `replace_paragraph_runs` with the hanging indent.
- The style toolbar's list toggles reflect "this is a bulleted/numbered item"
  (from read-only re-detection of the leading marker) while the editor is open.
- **Emptying the item** deletes it like any emptied paragraph — no special
  marker bookkeeping, because the marker was just text (Q6 resolved by Option B).

### 6.3 Creating a list

Two paths, both reusing existing machinery:

- **From existing text** (L2): select paragraphs with the tools that already
  exist — Ctrl/Shift+click or the box marquee (`_multi_paragraphs`) — then click
  the Bulleted/Numbered toggle. One undoable `make_list` command markers each
  selected paragraph and sets the hanging indent; numbering follows selection
  order. Unlisting is the same selection + toggle-off.
- **From scratch** (L3): Edit → "Insert list…" arms a click-to-place gesture
  (`arm_insert_point` + a persistent chip via `theme.armed_chip_qss()`, exactly
  like "Insert text…"). The click opens the paragraph editor seeded with the
  first marker's kind/style (from the sticky dropdown). Committing an item and
  pressing Enter-for-next is the L3 continuation behaviour; an empty item ends
  the list. (Continuation detail is the main L3 design work.)

### 6.4 Indentation (Tab / Shift+Tab, no drag)

- **One indent unit = 18 pt** (the marker→text gap in the samples; a sane step).
  Increase/Decrease-indent shift the item's `x_m` and `x_t` together by one unit
  and re-lay via `replace_paragraph_runs` — a pure geometry change, one undoable
  command, no text change.
- **Tab / Shift+Tab step the indent** (word-processor convention), via a small
  key handler on the paragraph editor gated to list items. This is the whole of
  "nesting" under Option B: a deeper hanging indent, nothing renumbered.
- **No drag** (decided) — Tab/Shift+Tab and the toolbar buttons are the only
  indent controls. No ruler, no tab stops.

### 6.5 What this does NOT add

No ruler, no tab stops, no list-continuation across page breaks, no bullet
image/picture markers. Those are word-processor scope and stay out.

## 7. Scope decisions

**Resolved (2026-07-26):**
- **Q2 Renumbering → manual only.** No auto-renumber (follows from Option B).
- **Q6 Deleting an item → no special handling.** The marker is text; emptying
  the item deletes it like any paragraph.
- **Q7 Marker model → editable text (Option B).** See the §3 decision.
- **Q8 Indent → Tab/Shift+Tab + buttons, NO drag.**
- **Numbered-list sample provided** → `sample_lists.pdf` (bullets, inline
  numbers, and hanging-indent numbers), so detection is grounded on real data.

**Resolved since:**
1. **How far to go? → all of it.** L1, L2 and L4 shipped 2026-07-26; L3 shipped
   the same day. The whole plan is built.

**Still open:**
2. **Marker styles to OFFER when creating.** Detection already READS them all
   (`lists.leading_marker`: `1.` `1)` `(1)` `a.` `i.`, and every glyph in
   `BULLET_GLYPHS`), but creation only writes `•` and `1.`. Offering more needs
   a marker-text parameter on the creation ops — and for BULLET glyphs it means
   EMBEDDING a symbol font (probe: `◦ ▪ – ‣ ●` round-trip via
   `TextStyle(fontfile=…)`, and collapse to U+00B7 with every base-14 code).
   That is a policy call — automatic font matching never embeds; an explicit
   fontfile is documented as a deliberate user choice — so it needs asking
   before building. The numbered variants (`1)`, `(1)`, `a.`, `i.`) have no such
   obstacle and are the cheap half.
3. **Numbered hanging indent.** A numbered marker is still INLINE, so a wrapped
   numbered item's continuation lines start under the number rather than under
   the text. Bullets hang correctly. Same refinement as it was at L1.
4. **The ink-wide box.** The "known limitation" above — re-editing a created
   item to add a word is refused when another item sits below it. Deriving an
   inserted box's wrap width from its REGISTRY rect rather than its ink bbox
   would fix it for every inserted text box, not just lists.
5. **The L2/L4 bullet fingerprint** (pre-existing, unchanged): a paragraph
   formatted as a bulleted list stores a fingerprint the page never matches, so
   its registry box stops owning its line. Mild on its own (the item still reads
   via normal dict-block grouping). The obvious fix — reuse L3's
   `lines_in_box` — was tried and reverted: without the drawn-line `origins`
   L3 has, it absorbs same-baseline neighbours and corrupts them. A proper fix
   needs `replace_paragraph_runs` to report its per-line geometry.

## 8. Test strategy

- Pure detection tests on `document_with_hyperlink.pdf` (bullets) and
  `sample_lists.pdf` (bullets + inline numbers + hanging-indent numbers), both
  skip-if-absent; plus a synthetic fixture in conftest for CI portability.
- Round-trip tests for every mutating op (make/clear/insert), per rule 10.
- Reflow test: a font change on a wrapped list item re-wraps within its box.
- Boundary guard: `pdfcore/lists.py` imports no Qt.
