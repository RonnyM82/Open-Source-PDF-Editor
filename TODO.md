# TODO — LR5 hands-on pass on feat/lists

This is Scott's testing checklist before `feat/lists` merges to `main`. It is
LR5 in `docs/list-feature-plan.md`: docs are done, this file is the remaining
"Scott's hands-on pass" item. Delete this file once every item below is
checked off and the merge has happened — it is a working list, not a
permanent record. The permanent record of what shipped and why lives in
`docs/PLAN.md`, `docs/list-feature-plan.md`, and `docs/box-width-plan.md`.

Two real bugs already came out of this pass and are fixed and tested
(commit `de630ec`): a merge of the four numbered items on
`samples/sample_lists.pdf` page 2 was refused because the merge pitch used
to take the union's median line advance (25 pt, the gap between the boxes)
instead of the items' own line spacing (17 pt); and a third-level roman
list renumbered `i., ii.` to `ix., x.` because a single-letter marker like
`i.` is ambiguous between the 9th letter and the roman numeral 1, and only
the block's indent level can say which. The rest of this list is what
turned up in the review that followed those two reports, still unverified
by hand.

## Merge and duplicate, beyond the confirmed fix

- [ ] Merge a numbered list whose items sit at different indent levels
      (`1.`, `2.`, a nested `a.`, back to `3.`) into one box. The pitch fix
      reads each member's own intra-box line advances, and a member that
      itself has lines at more than one indent level is a case the fix
      was not tested against.
- [ ] Merge two separate numbered lists (two boxes, each already
      numbered `1., 2.`) into one box, and confirm the result renumbers
      sequentially rather than keeping two `1, 2` runs side by side.
- [ ] Merge a list box with a plain paragraph, and separately merge a
      bulleted box with a numbered box. CLAUDE.md documents that a
      mixed or native merge "dissolves to plain content" — confirm that
      is what actually renders, not a half-formatted mix of markers and
      prose.
- [ ] Duplicate a list box and confirm the copy lands with real markers,
      correct ordinals, and a cascade offset that does not overlap or
      absorb the original's items. Note that the copy is EXPECTED to
      restart its own numbering rather than continue the original's —
      that is Acrobat's per-box model, not a bug, so a duplicated
      "1. 2. 3." producing a second "1. 2. 3." is correct.
- [ ] Ctrl+drag a list box across the page and confirm the markers and
      hanging indent survive the move rather than flattening to inline
      "1. text" with no hang.
- [ ] Group-move a multi-selection that includes a list box alongside
      plain boxes.

## Bulleted lists

Everything reported so far has been numbered lists. Bulleted lists share
the same engine path but have not been separately exercised:

- [ ] Toggle bullets on and off on a plain paragraph.
- [ ] Nest a bulleted list two or three levels deep and confirm the
      glyph cycles `•` `◦` `▪` per level.
- [ ] Duplicate and merge a bulleted box the same way the numbered-list
      items above were tested.

## Live-editor mechanics not yet tried

- [ ] Tab and Shift+Tab to nest and un-nest an item from the keyboard,
      not just the toolbar indent buttons.
- [ ] Press Enter on an empty list item and confirm it ends the list and
      returns to plain text, rather than continuing with an empty marker.
- [ ] Press Backspace at the very start of an indented item and confirm
      whether outdenting it feels natural or gets in the way. This is
      open question 2 in `docs/list-feature-plan.md`, and the plan says
      to drop the behaviour if the hands-on pass finds it more annoying
      than useful — this is the pass that decides it.

## Deep nesting

- [ ] Build a list four or five levels deep. The marker style cycles
      decimal, then alpha, then roman, every three levels, so a deep
      list is also the best place to confirm the roman-ordinal fix holds
      up alongside that cycle rather than only at level 2 where it was
      tested. Also check that trying to indent past the deepest level
      gives a refusal that reads sensibly rather than a confusing error.

## Persistence

- [ ] Create a list, save, close the file, and reopen it. Click into an
      item and confirm the toolbar shows the bulleted or numbered toggle
      as checked, the way it would for a list that was never closed.
- [ ] Open the saved file in a second viewer — a browser's PDF viewer or
      Acrobat, whichever is at hand. This is the check that actually
      validates the redesign: the entire rebuild happened because the
      old bullets were invisible in a real render, not in this app's own
      renderer, so this app's own render passing proves nothing on its
      own.
- [ ] Undo and redo back through a sequence of list edits — toggle,
      then indent, then merge — and confirm each is its own single step.

## The box-width fix, outside lists

- [ ] Insert a short plain text label above other content, save and
      reopen the file, then add a sentence to it. Confirm it wraps
      inside the box instead of being refused — this is the original
      bug report, tested here without a list involved.
- [ ] Do the same with a right- or centre-aligned box specifically, and
      confirm the text does NOT slide across the page when you commit
      the edit. This one was only unit-tested, never looked at on
      screen, and list boxes cannot exercise it (list layout is always
      left-set), so a plain aligned text box is the only way to see it.

## Two design calls that are yours to make

- [ ] Insert a short text box with a lot of open page to its right and
      reopen it for editing. The editor now opens as wide as the box is
      allowed to grow, which for a three-word box on an empty page can
      be most of the page width. Decide whether that reads as too much
      chrome — if so, capping it is a one-line change (open question 1,
      `docs/box-width-plan.md` section 7).
- [ ] Drag a text box's right edge out over existing content on purpose,
      then commit an edit. The app currently allows this without warning
      you the new text may overprint what is underneath. Decide whether
      that should raise a warning instead of staying silent (open
      question 2, same section).
