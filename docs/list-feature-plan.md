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

### L3 — Creation & continuation

Goal: start a list from scratch; Enter continues it.

- Insert-list armed mode (like insert-text): click to place, the paragraph
  editor opens seeded with the first marker (from the sticky marker-style
  dropdown). Enter starts the next item with the next marker/number as literal
  text; an empty item ends the list.
- Numbering the freshly typed items is a convenience only (still literal text —
  no ongoing management).

> **L3 KICKOFF NOTE (2026-07-26 — L1/L2/L4 shipped, L3 is the last piece).**
> Start here; it's self-contained. What already exists to build on:
> - `textedit.set_list_style(doc, n, para, kind, ordinal=)` CREATES a list item
>   from a paragraph (draw-marker path when there's no kept bullet) with the
>   hanging indent. `list_item_kind(para)` classifies an existing item. These
>   are the creation primitives — L3 is mostly a UI gesture on top.
> - **Engine gap:** `insert_new_runs` (the free-text insert path) has NO hang
>   support — only `replace_paragraph_runs` does. So the cleanest L3 insertion
>   is: insert a normal paragraph at the click (`insert_new_text`/runs), then
>   immediately `set_list_style(..., "bullet"/"number")` on it — reuse, don't
>   add a parallel hang path to `insert_new_runs`.
> - **The armed gesture** mirrors Edit → "Insert text…" (`arm_insert_point` +
>   `theme.armed_chip_qss()` chip; `_on_insert_point`). Seed the marker from the
>   sticky marker-style dropdown (build it with `MainWindow._make_dropdown_
>   button`, same as the alignment control — see §6.1).
> - **Enter-continuation is the real work** and the only hard part: on a plain
>   Enter (not Ctrl+Enter) commit the current item AND re-arm an insert one
>   pitch below with the next marker; an empty item ends the list. The paragraph
>   editor currently treats Enter as a line break and Ctrl+Enter as commit
>   (`_para_editor`), so continuation needs a list-mode branch in the editor's
>   key handling — scope this carefully or ship insert-one-item first and add
>   continuation second.
> - **Testing:** offscreen, drive the dispatch (`_on_insert_point`-style) like
>   the other UI tests; assert the inserted item folds (`hang_indent > 0`) and
>   round-trips. A synthetic doc is enough; no new fixture needed.

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

**Still open:**
1. **How far to go?** L1 (grouping fix) only, L1+L2 (format existing), or through
   L3 (create)? L4 is now trivial (Tab/Shift+Tab indent) and folds into L2/L3.
2. **Numbered-marker styles** to RECOGNISE/OFFER: just `1.`, or also `1)`, `a.`,
   `i.`, `(1)`? (Detection should read all; creation needs a chosen default.)

## 8. Test strategy

- Pure detection tests on `document_with_hyperlink.pdf` (bullets) and
  `sample_lists.pdf` (bullets + inline numbers + hanging-indent numbers), both
  skip-if-absent; plus a synthetic fixture in conftest for CI portability.
- Round-trip tests for every mutating op (make/clear/insert), per rule 10.
- Reflow test: a font change on a wrapped list item re-wraps within its box.
- Boundary guard: `pdfcore/lists.py` imports no Qt.
