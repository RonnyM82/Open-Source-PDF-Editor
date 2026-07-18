# Architecture

A standalone Windows PDF viewer + editor built on **PyMuPDF** (engine) and
**PySide6** (UI). This document is the map a new contributor should read first:
the layering rule, the coordinate model, and the handful of design decisions
that everything else follows from.

## The hard boundary (most important rule)

Two layers, strictly separated:

- **`src/pdfcore/`** — the headless engine. Pure Python + PyMuPDF. **It never
  imports Qt or PySide6.** Every PDF operation (open, render-to-bytes, page ops,
  text/image editing, save) lives here as functions and dataclasses with plain
  inputs and outputs, fully testable with pytest and no GUI in the loop.
- **`src/pdfapp/`** — a thin PySide6 UI. It imports `pdfcore` only, wiring
  widgets to engine calls and rendering results. It holds no PDF logic of its
  own.

A guard test greps `src/pdfcore` for any `PySide6` / `qt` import and fails the
build if one appears. Keep the dependency arrow pointing one way: `pdfapp →
pdfcore`, never the reverse.

### The single Qt↔engine seam

`pdfapp/qt_image.py` turns a `RenderedPage` (a plain dataclass carrying
`width, height, stride, samples: bytes, channels`) into a `QImage`. The engine
renders to raw bytes; the UI wraps them:

```python
QImage(samples, w, h, stride, fmt).copy()
```

The `.copy()` is mandatory — `QImage` does not take ownership of the buffer, so
without it the bytes can be garbage-collected out from under Qt, giving a
dangling pointer and a crash. `channels` (1/3/4) selects the format
(`Grayscale8` / `RGB888` / `RGBA8888`).

## Coordinate model

Rotation is the subtlety in any PDF editor. This app confines all
rotation-awareness to **one module**, `pdfapp/page_coords.py` (pure — no Qt, no
PyMuPDF at runtime). Three spaces:

- **Page space** — unrotated PDF points, top-left origin, y-down. This is what
  PyMuPDF's `get_text("dict")`, `add_redact_annot`, and `insert_text` all speak,
  and it is the *only* space the engine knows. All of `pdfcore` is
  rotation-blind.
- **Scene space** — rendered-pixmap pixels (page rotation applied by
  `get_pixmap`, times the render zoom; width/height swap at 90°/270°).
- **Viewport space** — Qt's own `mapToScene` / `mapFromScene` territory; it
  never enters the coordinate module.

`scene_to_page` / `page_to_scene` and friends convert between the first two;
the engine stays clean because the UI translates at the boundary.

## Rendering and the render cache

The engine's render is **stateless** — `render_page(n, zoom|dpi)` returns bytes
and caches nothing. Caching lives entirely in the UI (`pdfapp/render_cache.py`),
a byte-budgeted LRU keyed by `(page_index, kind, zoom-or-dpi)`.

Two rules make caching safe:

1. **Resolution-exact rendering.** The main view renders at exactly
   `zoom × devicePixelRatio` (capped), so at rest one render pixel maps to one
   device pixel with no resampling. Rasterising at the target resolution — rather
   than scaling a fixed-DPI bitmap — is what keeps small text crisp.
2. **Clear-on-mutation.** Any structural change (delete, reorder, insert, merge,
   save-as) and *every* undo/redo restore clears the whole cache; page-scoped ops
   (rotate, a text edit) may evict just the affected page. A stale render showing
   the wrong page is the bug this prevents. Three per-page caches (render,
   geometry, OCR words) all ride the same invalidation funnel.

## Undo — snapshot-based

Undo is UI-side (`pdfapp/undo.py`), one `QUndoStack` per open document. A command
runs the engine op once, captures before/after document **snapshots** (compressed
in-memory bytes), and every later undo/redo is a pure byte-for-byte restore.

The key invariant: **all mutations flow through the undo stack.** A snapshot
restore replaces the whole document, so any edit that bypassed the stack would be
silently resurrected by a later undo. Undo restores document *state* — it does
not reverse a PDF operation (see redaction below).

## Text editing without reflow

Editing existing text is redaction-and-reinsert:

1. Extract spans with `get_text("dict")` — font, size, colour, flags, bbox,
   baseline origin, all in unrotated page space.
2. Redact a thin band around the target's baseline (not the full bbox — char
   boxes span the whole line height, so a bbox-sized redaction eats the line
   above), then `apply_redactions`.
3. Reinsert the replacement with `insert_text` at the original baseline, mapping
   the font to a **non-embedded base-14 code** at the original size/colour.

Redaction is **destructive at the PDF level** and geometric: it removes every
glyph whose box intersects the band, including unrelated text that happens to
overlap. The engine therefore captures overlapping "foreign" spans before
redacting and repairs them after, so an edit removes only the text it meant to.
Because reinserted text is non-embedded base-14, every viewer substitutes old and
new text identically — consistent fidelity rather than chasing pixel-parity with
one viewer.

Paragraph editing re-wraps replacement text **within the original paragraph's own
box**, reproducing the line pitch. That is a bounded convenience, not reflow:
nothing outside the box moves and no cross-paragraph layout is recomputed. Full
reflow, a document-wide paragraph model, and form filling are deliberately out of
scope.

## Font mapping

Standard families (Helvetica/Arial, Times, Courier) map to base-14 codes;
name substrings drive the bold/italic signal, span flag bits are the fallback.
Replacement text is reinserted **non-embedded** so viewers substitute it the same
way they substitute the original. If a span's font is genuinely *embedded*, the
app flags it ("font can't be matched exactly") rather than silently degrading —
embedded-font reproduction is out of scope.

## Editing model (UI)

Documents open **read-only**; an explicit Edit mode gates every editorial entry
point. In edit mode, editable regions are outlined, hover names the exact gesture
in the status bar, selection is click-first (a stray drag never moves text), and
the in-place editors float over the page with deliberately light chrome so a dark
app theme doesn't bleed a dark box over the white page. Every affordance ships a
read-only inertness test.

## Printing

The engine exposes one method — `render_page_at_dpi(n, dpi, gray=False)`,
stateless — and the UI (`pdfapp/print_support.py`) owns `QPrinter` / `QPainter`
and the target-rect maths. Each page is re-rendered at the printer's real DPI, one
page at a time — never scale the screen bitmap (that is the blurry-print bug).
Greyscale is rendered in the engine (`csGRAY`), not desaturated in the driver.
Orientation is resolved per page by default. `pdfcore` never imports Qt or
QtPrintSupport.

## Theming

`pdfapp/theme.py` is the single styling authority: **qt-material** provides the
app-wide dark/light Material theme, with a small hand-written QSS addendum layered
on top. Icons come from exactly one source (`pdfapp/icons.py`, QtAwesome / Material
Design Icons). The rendered PDF page is a pixmap — no stylesheet touches it, so it
stays true white in every mode.

## OCR (read-only)

Pages with no text layer (scans, or text exported as vector outlines) are handled
by `pdfcore/ocr.py`, which drives an external **Tesseract** binary via
`pytesseract` and returns word boxes in unrotated page space — the same space as
`TextSpan`. OCR is strictly a *read* path: Extract Text and OCR-assisted search
consume it, but no OCR text layer is ever written back into a document. The engine
never caches OCR and never runs it implicitly; callers supply OCR words explicitly.

## Packaging

PyInstaller one-folder build (`pdf_editor.spec`, `console=False`). The native
MuPDF binaries ship via `collect_dynamic_libs("pymupdf")`; the Tesseract runtime
(exe + its DLL closure + English model data) is staged into the bundle as
application assets. `scripts/package.ps1` builds and zips it; an Inno Setup script
wraps the bundle into a per-user installer. Resource lookups use the
`sys._MEIPASS` / `sys.frozen` pattern so paths work both from source and frozen.

## Testing

pytest, src-layout, `--import-mode=importlib`. `conftest.py` generates every
fixture programmatically with PyMuPDF (text pages, images, multi-page, encrypted,
a gridline-table "quote" page, an embedded-font page, and no-text-layer OCR
pages). Engine ops are covered by round-trip tests (open → operate → save →
reopen → assert). A boundary guard test fails if `pdfcore` imports Qt. Tests that
need Tesseract skip cleanly when the binary is absent. Sample PDFs under
`samples/` are sanitised, fabricated documents used by a few gate tests; the
directory is otherwise gitignored so no real document is ever committed.
