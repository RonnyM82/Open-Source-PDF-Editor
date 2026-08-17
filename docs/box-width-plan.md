# Inserted text boxes get an honest wrap width

Status: **BUILT, suite green** (2026-08-18). BW1 and BW2 landed in one engine
commit, BW3 wired the UI, BW4 is the documentation. Written on `feat/lists`,
but the defect and the fix are general to every inserted text box, so this
lands independently of the list feature. It closes open question 3 in
`docs/list-feature-plan.md` section 8, where it was recorded as the sharpest
remaining edge on that branch. Section 7's open questions survive the build and
are what Scott's hands-on pass should look at.

## 1. The defect, reproduced

Insert a short text box, re-open it, add two words, and the commit is refused:

```
inserted lines=('Terms of trade',)
registered rect=(72.0, 107.32, 130.52, 119.69)  width=58.52 pt
page width 595, so ~464 pt of blank space to the right
paragraph text='Terms of trade'  bbox width=58.52 pt

REFUSED: The edited text needs more lines than the paragraph box has, and the
space it would grow into is already occupied by other text ...
```

The repro puts a two-line paragraph one pitch below the inserted box, which is
the ordinary case: a label typed above existing content. Re-committing the same
text with `width=460` instead lays "Terms of trade apply here" out on ONE line,
leaves the paragraph below untouched, and is not refused. So nothing about the
edit is impossible. The box simply believes it is 58 pt wide.

Scott's words for the user experience: you cannot add a word to a text box you
created five minutes ago. The workaround is dragging the paragraph editor's
right edge to set a wrap width, which nobody discovers, and which is forgotten
again the moment the editor closes.

## 2. Why it happens

Three separate pieces of correct-looking behaviour compose into the defect.

1. The insert commit registers the box with the INK bbox of the spans it just
   created (`document_view.py`, the `insert_op` span-diff at the "Insert text"
   command). A single typed line therefore registers a box exactly as wide as
   that line's glyphs.
2. `replace_paragraph_runs` derives its wrap width from the paragraph's own
   bbox (`wrap = para.bbox[2] - para.bbox[0]`) unless the caller passes an
   explicit `width`. The paragraph bbox is the union of its span bboxes, which
   is the ink again. So the re-edit wraps at the width of the text that
   happened to be typed the first time.
3. Adding a word therefore manufactures a second line, and the E9.4
   growth-collision check refuses to grow into space occupied by other text.

Every one of those three is worth keeping. The bug is that step 2 has no idea
what width the box was MEANT to have, so it falls back to measuring the ink.

There is a second, quieter defect in the same area. When an edit does carry an
explicit width, `new_bbox` is reported as `(origin_x, top, origin_x + wrap,
bottom)`, so the registry rect balloons to the wrap width even when the text
inside it is short. The probe above records a 460 pt rect around 103 pt of ink.
That rect is what box ownership hit-tests against, so a generous wrap width
would inflate every inserted box's claim over the page. Section 3 treats this
as part of the fix rather than as a separate ticket.

## 3. Where the intended width should live

The suggestion carried in the list plan was to derive the wrap width from the
registry rect. Reviewing that: the registry rect is the wrong carrier, for two
reasons.

- The rect is what ownership is decided from. `_line_region` (engine) and
  `_box_for` (UI) both ask which registered box contains a line's or a
  paragraph's centre, and resolve overlaps by content fingerprint. Widening
  every inserted box's rect to a generous intended width widens what each box
  geometrically claims. The fingerprint protects a box that HAS one, because a
  foreign line under it returns -1 and stays in its own dict block, but legacy
  fingerprint-less records own by pure geometry, and side-by-side boxes with
  identical text become harder to tell apart. There is no reason to spend that
  risk on a value we can carry explicitly.
- The rect already means "where this box's ink is" on the insert path, and the
  ink extent is what `lines_in_box` reads a fingerprint back from. Keeping one
  meaning for the rect on both paths is worth more than reusing the field.

So the plan splits the two ideas apart:

| Concept | Carrier | Meaning |
| --- | --- | --- |
| Where the box's text is | `BoxRecord.rect` | The ink extent, on the insert path and after an edit. Unchanged semantics, ownership keeps working as it does today. |
| How wide the box may set text | computed per edit, plus `BoxRecord.width` when the user has dragged one | The width available to the box on the page now, or the width the user deliberately chose. |

Computing the automatic width per edit rather than storing it at insert time is
deliberate, and it buys two things. Boxes that already exist in Scott's files
get the fix retroactively, with no migration. And the answer stays true as the
page changes around the box, which a value frozen at insert time would not.

`BoxRecord.width` exists only to persist a width the user chose by dragging the
editor's right edge. That is the second half of the complaint: today the drag
is the workaround, and it is thrown away as soon as the editor closes. A stored
width also keeps a deliberately narrow box narrow, which the automatic
calculation would otherwise widen behind the user's back.

## 4. How the automatic width is calculated

A new engine function, `textedit.available_wrap_width(doc, page_index, para)`,
answers one question: how wide may this paragraph set text without printing
over anything to its right?

- Start from the page's right margin, `page_w - 2.0 - para.bbox[0]`, matching
  the margin `insert_new_runs` already uses for free-standing text.
- Scan every other span on the page (`extract_spans`, which already excludes
  review-comment text) and every image (`imageedit.images_on_page`). An
  obstacle counts when its vertical band overlaps one of the paragraph's own
  member bands AND it starts to the right of the paragraph's left edge. The
  nearest such obstacle, minus a small clearance, caps the width.
- Vertical bands use the same convention as the E9.4 collision check,
  `origin_y - 0.8 * size` to `origin_y + 0.25 * size`, rather than span
  bboxes. This matters: span boxes run the full ascender-to-descender height of
  the line, and real documents set lines tighter than their font metrics (the
  sample quote's description lines overlap by 2.39 pt), so a bbox test would
  let the line ABOVE a box veto its width. Using the same rule as E9.4 also
  means the horizontal guard and the vertical guard agree about what counts as
  overlapping text.
- The result is floored at the paragraph's own ink width. Never return less
  than the text already occupies, or the very next commit would re-wrap text
  that was fitting perfectly, which is the same class of bug the existing 2%
  wrap grace was added to prevent.

The floor is what makes this safe on a box that is already wider than its
surroundings allow. Such a box keeps its current width and gains nothing, and
its next edit is refused by E9.4 exactly as it is today, which is honest: there
really is no room.

## 5. What must not change

- **The E9.4 growth-collision refusal stays exactly as it is.** The fix is
  giving boxes an honest width, not weakening the check. A box hemmed in on
  both sides still refuses, and the tests that pin that keep passing.
- **Pre-existing document paragraphs keep deriving their wrap from their own
  bbox.** A quote's description paragraph has a real column width and widening
  it would re-wrap real documents. The automatic width applies only where the
  UI can prove the paragraph belongs to a registered box, which is the same
  `_box_for` call the commit already makes to keep the registry in step.
- **A deliberate drag stays authoritative and stays unclamped.** Today a
  dragged width can overlap content to the right, and the user is looking at
  the box while they drag it. Persisting that width does not change its
  semantics.
- **The plain insert path keeps laying text out with hard breaks only.** No
  wrap width is introduced at insert time. That is a real gap (a long typed
  line still runs past the editor's own width) but it is a different fix, and
  it is noted as open question 3 below rather than smuggled in here.

## 6. Milestones

Each milestone is committed green on its own, per repo rule 12.

### BW1: the engine calculates an available width

`textedit.available_wrap_width` plus the ink-based `new_bbox` correction in
`replace_paragraph_runs`. Tests in `tests/test_textedit.py`:

- The width reaches the page margin when the box has open space to its right.
- A span to the right, inside the box's band, caps the width at that span's
  left edge less the clearance.
- A span above-right or below-right, outside the band, does NOT cap it. This is
  the tighter-than-metrics case from section 4, and the fixture reproduces the
  2.39 pt line overlap deliberately.
- An image to the right caps it.
- The paragraph's own spans never cap it.
- The result is never below the paragraph's own ink width, including when an
  obstacle already overlaps the box.
- `new_bbox` hugs the laid-out ink even when the wrap width is far wider, so a
  short edit in a wide box no longer registers a page-wide rect.
- Round-trip, per repo rule 10: insert a box, register it, re-edit it through
  the wider width, save, reopen, and assert the text is one line, the box's
  registry entry still owns it, and the paragraph below is untouched.
- The refusal still fires when the box is genuinely hemmed in.

### BW2: the registry carries a chosen width

`BoxRecord.width` (default 0.0, meaning none chosen), written by
`add_box`/`update_box`, read back by `read_boxes`, and carried through the
`PdfDocument` wrappers. Tests in `tests/test_boxregistry.py`:

- A width round-trips through `write_boxes`/`read_boxes` and through a save and
  reopen.
- A record written without a width (the legacy shape, and every record already
  in a user's files) reads back as 0.0 rather than raising.
- `update_box_rect` preserves the width, the way it already preserves the
  fingerprint, because a move changes neither.
- Page remapping preserves it.

### BW3: the UI uses the honest width

In `document_view.py`:

- `_commit_paragraph_edit` resolves the box first, then passes
  `width = box.width or available_wrap_width(...)` for a registered box, and
  keeps passing the user's dragged width when there is one.
- A dragged width is stored on the box, so the next edit of that box starts
  from it.
- `_begin_paragraph_edit` opens the editor at the same width it will commit at,
  so what the user sees wrapping is what wraps.
- Pre-existing paragraphs keep passing `None`.

Tests in a new `tests/test_ui_box_width.py`, driven offscreen through the
dispatch methods the way the existing UI tests are:

- Insert a box above a paragraph, edit it, add two words, and the commit
  succeeds on one line with no refusal. This is section 1's repro as a test.
- A pre-existing paragraph's edit still wraps to its own box, so real documents
  are unaffected.
- A dragged width persists and is honoured on the following edit.
- The editor's opening width matches the width the commit uses.

### BW4: documentation

CLAUDE.md's box-registry and paragraph-editing sections gain the width rule,
`docs/PLAN.md` gets the milestone record, and open question 3 in
`docs/list-feature-plan.md` is closed with a pointer here.

## As built

The milestones landed as planned, with two details worth recording.

BW1 and BW2 went in as ONE commit rather than two. Both change
`pdfcore/document.py`, and splitting that file would have left an intermediate
commit where the wrappers call a registry that cannot take a width yet. A
broken snapshot is worse for bisecting than a slightly larger commit.

The paragraph editor's opening width needed a viewport clamp that the plan did
not anticipate. `_fit_to_content` only ever clamps the width it derives from
the CONTENT, so a rect wider than the viewport (a generous width at high zoom)
would have opened the editor past the right edge of the window. It is capped at
the viewport now, which means at high zoom the editor can show more wrapping
than the commit will produce. That is the pre-fix situation for that one case,
and it is better than an editor running off screen.

Test counts: 17 engine tests (BW1 and BW2), 6 UI tests (BW3), whole suite 1215
green. `scripts/lint.ps1` and `scripts/test.ps1` clean.

## 7. Open questions

1. How wide should the paragraph editor actually open for a short box with a
   whole page to its right? Matching the commit width is the honest answer and
   is what BW3 does, but a 460 pt white overlay around three words may read as
   too much chrome, and the overlay is opaque, so it hides whatever sits beside
   the box while it is open. Capping it is a one-line change if Scott's
   hands-on pass says so.
2. Should a deliberately dragged width be obstacle-clamped after all? It is not
   clamped today, and section 5 keeps that, but a user who drags a box over a
   table and then edits it gets silent overprinting. The honest fix is probably
   a warning rather than a refusal, and it belongs with whatever else comes out
   of the hands-on pass.
3. The plain insert path still does not wrap, so a long line typed into the
   insert editor commits past the editor's own right edge. The available-width
   calculation is exactly what that path would need, so this is now a small
   fix, but it changes insert behaviour and needs its own decision.
