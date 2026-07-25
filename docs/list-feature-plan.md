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

### L1 — Recognition & clean editing (the grouping fix)

Goal: a list item edits as ONE unit — marker stays, hanging indent preserved.

- `pdfcore/lists.py`: `ListMarker(kind, text, bbox, ordinal)`,
  `ListItem(marker, body_spans, marker_indent, text_indent, bbox)`,
  `detect_lists(doc, n) -> list[ListItem]` (pure).
- Integrate with the **box/paragraph model**: `paragraph_at` / `paragraphs`
  treat a detected item's marker+body as ONE `Paragraph` whose first line's
  "text indent" is the hanging indent and whose marker is carried alongside
  (so a re-layout re-emits the marker at `x_m` and the text at `x_t`). This
  reuses the existing `_partition_lines` / box-region machinery — a list item
  becomes a first-class region, the same way a registered box does.
- Editing the item text (`replace_paragraph_runs`) re-lays the body at the
  hanging indent and re-draws the marker at the marker indent; deleting the
  text can optionally drop the marker (open question 6).
- No creation, no renumber. Markers are preserved verbatim (numbered items keep
  their literal number — we do not renumber yet).

Tests: detection on `document_with_hyperlink.pdf` (3 bullet items grouped),
synthetic numbered-list fixture, round-trip edit keeps marker + hanging indent.

### L2 — Format existing paragraphs as a list / unlist

Goal: select paragraphs → "Bulleted list" / "Numbered list" toggle.

- Engine `make_list(doc, n, paragraphs, kind, start=1)`: prepend a marker to
  each paragraph (bullet glyph, or `1.` `2.` … for numbered), set the hanging
  indent (shift body to `x_t`, marker at `x_m`). Uses the runs engine — additive
  markers + re-laid body; one undoable command.
- Engine `clear_list(doc, n, items)`: remove markers, restore the flat indent.
- UI: two toolbar toggles on the Text style toolbar (icons
  `format-list-bulleted` / `format-list-numbered`), enabled on a multi-select or
  an open paragraph editor. Numbered lists number in selection order.
- Manual renumber only (editing an item does not renumber siblings yet).

Tests: format 3 paragraphs → 3 items with correct markers/indent; unlist
restores; round-trip.

### L3 — Creation & continuation

Goal: start a list from scratch; Enter continues it.

- Insert-list armed mode (like insert-text): click to place, type items, Enter
  starts the next item with the next marker/number, empty item ends the list.
- Uses `insert_new_runs` with marker + hanging-indent layout.
- Auto-number within the newly created list (renumber-on-edit for EXISTING lists
  stays out — see L4).

### L4 — Live list behaviour (explicitly bounded — needs a scope decision)

Auto-renumber on item add/remove/reorder, Tab/Shift+Tab nesting, marker style
picker (`a.` vs `1.` vs `•` vs `◦`). This is the part that edges toward a reflow
word processor (renumber ripples down; nesting recomputes indents). Recommend
deferring and deciding per-capability rather than committing up front.

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

## 6. Open scope questions (for the user)

1. **How far to go?** L1 (grouping fix) only, L1+L2 (format existing), or through
   L3 (create)? L4 (auto-renumber/nesting) recommended deferred.
2. **Renumbering**: manual only, or auto-renumber when items are added/removed?
   Auto-renumber is the biggest complexity jump.
3. **Nesting** (multi-level lists): in scope or deferred?
4. **Numbered-marker styles**: just `1.`, or also `1)`, `a.`, `i.`, `(1)`?
5. **A real sample with a NUMBERED list** would de-risk detection — the current
   samples are bullets only. Can one be provided?
6. **Deleting an item's text**: drop the marker too, or leave an empty bullet?

## 7. Test strategy

- Pure detection tests on `document_with_hyperlink.pdf` (bullets) + a synthetic
  numbered-list fixture built in conftest (embed a marker + hanging text).
- Round-trip tests for every mutating op (make/clear/insert), per rule 10.
- A skip-if-absent test on any real numbered-list sample once provided.
- Boundary guard: `pdfcore/lists.py` imports no Qt.
