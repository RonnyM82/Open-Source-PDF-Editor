"""The PdfDocument session object — open, inspect, render, save.

Pure PyMuPDF; no Qt. Page-manipulation operations live in pdfcore/pages.py
(added from M6). See CLAUDE.md for the boundary rule and the save constraint.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path

import pymupdf

from pdfcore import (
    boxregistry,
    comments,
    imageedit,
    invoice,
    ocr,
    pages,
    textedit,
    textselect,
    textsource,
)
from pdfcore.boxregistry import BoxRecord
from pdfcore.comments import CommentInfo
from pdfcore.imageedit import ImageInfo
from pdfcore.invoice import InvoiceExtract
from pdfcore.ocr import OcrWord
from pdfcore.render import RenderedPage, render_page, render_page_at_dpi
from pdfcore.textedit import Paragraph, StyledRun, TextSpan, TextStyle
from pdfcore.textsource import PageText


class PdfDocument:
    """A thin, headless wrapper around a PyMuPDF document.

    Open with :meth:`open`. Use as a context manager to close cleanly. The
    engine accepts a password but never prompts for one — the UI supplies it.
    """

    def __init__(self, doc: pymupdf.Document, source: Path | None = None) -> None:
        self._doc = doc
        self._source = source

    @classmethod
    def open(cls, path: str | Path, password: str | None = None) -> PdfDocument:
        source = Path(path)
        doc = pymupdf.open(source)
        if doc.needs_pass and password is not None:
            doc.authenticate(password)
        return cls(doc, source=source)

    @property
    def source(self) -> Path | None:
        """The path this document was opened from (None if not from a file)."""
        return self._source

    # --- encryption -----------------------------------------------------
    @property
    def needs_pass(self) -> bool:
        """True if the document is password-protected.

        PyMuPDF keeps this truthy even after successful authentication — it
        reports that the file *is* encrypted, not the current auth state.
        """
        return bool(self._doc.needs_pass)

    def authenticate(self, password: str) -> bool:
        """Try ``password``; return ``True`` on success."""
        return bool(self._doc.authenticate(password))

    # --- inspection -----------------------------------------------------
    @property
    def page_count(self) -> int:
        return self._doc.page_count

    def page_size(self, n: int) -> tuple[float, float]:
        """``(width, height)`` of page ``n`` in PDF points."""
        rect = self._doc[n].rect
        return (rect.width, rect.height)

    def page_rotation(self, n: int) -> int:
        """Current rotation of page ``n`` in degrees (0 / 90 / 180 / 270)."""
        return int(self._doc[n].rotation)

    # --- rendering ------------------------------------------------------
    def render_page(
        self,
        n: int,
        *,
        zoom: float = 1.0,
        dpi: int | None = None,
        alpha: bool = False,
    ) -> RenderedPage:
        return render_page(self._doc[n], zoom=zoom, dpi=dpi, alpha=alpha)

    def render_page_at_dpi(self, n: int, dpi: int, gray: bool = False) -> RenderedPage:
        """Render page ``n`` at an explicit DPI, optionally greyscale.

        Stateless/pure — no engine caching (used by printing; the UI owns any
        preview cache). Greyscale renders 1 byte/pixel.
        """
        return render_page_at_dpi(self._doc[n], dpi, gray=gray)

    # --- text (Phase 2) ---------------------------------------------------
    def text_spans(self, n: int) -> list[TextSpan]:
        """Text spans of page ``n``, in unrotated page coordinates."""
        return textedit.extract_spans(self._doc, n)

    def replace_text(
        self,
        n: int,
        span: TextSpan,
        new_text: str,
        *,
        fill: tuple[float, float, float] | bool = False,
        style: TextStyle | None = None,
    ) -> textedit.ReplaceResult:
        """Replace ``span``'s text on page ``n`` (tight redact + reinsert).

        Destructive at the PDF level — undo is snapshot-based (E3), it never
        un-redacts. Empty ``new_text`` deletes the span. ``style`` overrides
        the span's own style (the style toolbar); None matches the original.
        """
        return textedit.replace_span_text(self._doc, n, span, new_text, fill=fill, style=style)

    def insert_text(
        self,
        n: int,
        point: tuple[float, float],
        text: str,
        *,
        style: TextStyle | None = None,
        align: str = "left",
    ) -> None:
        """Insert NEW text at a baseline point on page ``n`` (additive).

        ``align`` justifies multi-line text against its widest line.
        """
        textedit.insert_new_text(self._doc, n, point, text, style=style, align=align)

    def paragraph_at(
        self,
        n: int,
        px: float,
        py: float,
        boundaries: Sequence[tuple[float, float, float, float]] = (),
    ) -> Paragraph | None:
        """The paragraph under an unrotated page point on page ``n``.

        ``boundaries`` (insert-box bboxes) isolate session-inserted text so it
        never merges with a neighbouring pre-existing paragraph.
        """
        return textedit.paragraph_at(self._doc, n, px, py, boundaries=boundaries)

    def paragraphs(
        self, n: int, boundaries: Sequence[tuple[float, float, float, float]] = ()
    ) -> list[Paragraph]:
        """Every paragraph on page ``n`` (unrotated page coordinates)."""
        return textedit.paragraphs_on_page(self._doc, n, boundaries=boundaries)

    def replace_paragraph(
        self,
        n: int,
        para: Paragraph,
        new_text: str,
        *,
        fill: tuple[float, float, float] | bool = False,
        offset: tuple[float, float] = (0.0, 0.0),
        style: TextStyle | None = None,
        width: float | None = None,
        align: str | None = None,
    ) -> textedit.ParagraphReplaceResult:
        """Replace a whole paragraph, re-wrapped within its own box.

        ``offset`` translates the box (page points) — same text + offset MOVES
        the paragraph. ``style`` overrides the dominant style (style toolbar).
        ``width`` overrides the wrap width (a resized editor). ``align``
        overrides the detected justification (None keeps it). Raises
        ValueError (before any mutation) if the text will not fit.
        """
        return textedit.replace_paragraph_text(
            self._doc,
            n,
            para,
            new_text,
            fill=fill,
            offset=offset,
            style=style,
            width=width,
            align=align,
        )

    # --- page operations (mutate the open document) ---------------------
    def rotate(self, page_nos: Iterable[int], deg: int) -> None:
        """Rotate the given pages BY ``deg`` degrees (multiple of 90)."""
        pages.rotate(self._doc, page_nos, deg)

    def delete(self, page_nos: Iterable[int]) -> None:
        """Delete the given pages (cannot delete every page)."""
        pages.delete(self._doc, page_nos)

    def reorder(self, order: Iterable[int]) -> None:
        """Reorder pages to a full permutation of the current page indices."""
        pages.reorder(self._doc, order)

    def insert_from(
        self,
        src_path: str | Path,
        at: int,
        from_page: int | None = None,
        to_page: int | None = None,
    ) -> None:
        """Insert pages from another PDF file at index ``at``."""
        pages.insert_from(self._doc, src_path, at, from_page, to_page)

    # --- persistence ----------------------------------------------------
    def save(self, path: str | Path) -> None:
        """Save a cleaned, compressed copy to a NEW path.

        Saving over the currently-open file is refused: PyMuPDF cannot rewrite a
        file it has open. The UI's Save-in-place (M12) does a temp-write +
        ``os.replace`` instead.
        """
        dest = Path(path)
        if self._source is not None and _same_path(dest, self._source):
            raise ValueError(
                "Cannot save over the currently-open file; save to a new path "
                "or use save_in_place()."
            )
        self._doc.save(str(dest), garbage=4, deflate=True)

    def save_in_place(self) -> None:
        """Atomically save changes back to the currently-open file.

        PyMuPDF cannot rewrite a file it has open, so we write a temp file in the
        same directory, close our handle, ``os.replace`` over the original, then
        reopen it (so this document keeps working after the save).

        Failure of the replace (typically WinError 5/32: the target is open
        in another application) must NOT strand the document: our handle is
        already closed at that point, so the document is restored from the
        TEMP bytes — which hold the unsaved edits; reopening from the source
        would silently discard them — and becomes stream-backed (same as a
        snapshot restore; a later save_in_place still works). The temp file
        is always cleaned up; the exception propagates for the UI to explain.
        """
        if self._source is None:
            raise ValueError("document has no source path; use save(path) instead")
        source = self._source
        tmp = source.with_name(source.name + ".tmp")
        try:
            self._doc.save(str(tmp), garbage=4, deflate=True)
        except BaseException:
            tmp.unlink(missing_ok=True)  # a partial temp is only litter
            raise
        self._doc.close()
        try:
            os.replace(tmp, source)
        except OSError:
            data = tmp.read_bytes()
            tmp.unlink(missing_ok=True)
            self._doc = pymupdf.open(stream=data, filetype="pdf")
            raise
        self._doc = pymupdf.open(source)

    def replace_text_runs(
        self,
        n: int,
        span: TextSpan,
        runs: list[StyledRun],
        *,
        fill: tuple[float, float, float] | bool = False,
    ) -> textedit.ReplaceResult:
        """Replace ``span`` with RICH runs (selection-level styling, E9)."""
        return textedit.replace_span_runs(self._doc, n, span, runs, fill=fill)

    def replace_paragraph_runs(
        self,
        n: int,
        para: Paragraph,
        runs: list[StyledRun],
        *,
        fill: tuple[float, float, float] | bool = False,
        offset: tuple[float, float] = (0.0, 0.0),
        width: float | None = None,
        align: str | None = None,
    ) -> textedit.ParagraphReplaceResult:
        """Replace ``para`` with RICH runs (per-word styles preserved, E9).

        ``align`` overrides the detected justification (None keeps it).
        """
        return textedit.replace_paragraph_runs(
            self._doc, n, para, runs, fill=fill, offset=offset, width=width, align=align
        )

    def insert_runs(
        self,
        n: int,
        point: tuple[float, float],
        runs: list[StyledRun],
        *,
        align: str = "left",
        pitch: float | None = None,
    ) -> None:
        """Insert NEW rich text at a baseline point on page ``n`` (E9).

        ``pitch`` overrides the default 1.2-em baseline spacing (a copy of an
        existing paragraph reproduces that paragraph's own pitch).
        """
        textedit.insert_new_runs(self._doc, n, point, runs, align=align, pitch=pitch)

    def highlight(
        self, n: int, span: TextSpan, color: tuple[float, float, float] | None = None
    ) -> None:
        """Add a highlight annotation over ``span`` on page ``n`` (E7).

        ``color`` is an ``(r, g, b)`` 0-1 stroke colour; ``None`` = yellow.
        """
        textedit.add_highlight(self._doc, n, span, color)

    def highlight_region(
        self,
        n: int,
        rect: tuple[float, float, float, float],
        color: tuple[float, float, float] | None = None,
    ) -> int:
        """Highlight the text inside a selection window on page ``n``.

        Character-level clipping; returns the number of annotations added.
        ``color`` is an ``(r, g, b)`` 0-1 stroke colour; ``None`` = yellow.
        """
        return textedit.highlight_region(self._doc, n, rect, color)

    def highlight_rects(
        self,
        n: int,
        rects: list[tuple[float, float, float, float]],
        color: tuple[float, float, float] | None = None,
    ) -> int:
        """Highlight per-line union rects (a text SELECTION) on page ``n``.

        One annotation per rect; returns the count. ``color`` is an
        ``(r, g, b)`` 0-1 stroke colour; ``None`` = yellow.
        """
        return textedit.highlight_rects(self._doc, n, rects, color)

    # --- inserted-box registry (E10; identity stored IN the document) ------
    def boxes(self, n: int | None = None) -> list[BoxRecord]:
        """Registered user-inserted boxes — all, or page ``n``'s only."""
        boxes = boxregistry.read_boxes(self._doc)
        return boxes if n is None else [b for b in boxes if b.page == n]

    def add_box(self, n: int, rect: tuple[float, float, float, float]) -> BoxRecord:
        """Register a newly inserted box on page ``n``; returns its record."""
        return boxregistry.add_box(self._doc, n, rect)

    def update_box_rect(self, box_id: str, rect: tuple[float, float, float, float]) -> None:
        """Record a registered box's new placement (after a move/resize)."""
        boxregistry.update_box_rect(self._doc, box_id, rect)

    def remove_box(self, box_id: str) -> None:
        """Drop a box from the registry (e.g. its text was deleted)."""
        boxregistry.remove_box(self._doc, box_id)

    # --- images (Phase 2, E6) ---------------------------------------------
    def images(self, n: int) -> list[ImageInfo]:
        """Image placements on page ``n`` (unrotated page coordinates)."""
        return imageedit.images_on_page(self._doc, n)

    def image_at(self, n: int, px: float, py: float) -> ImageInfo | None:
        """The image under an unrotated page point on page ``n``."""
        return imageedit.image_at(self._doc, n, px, py)

    def insert_image(
        self, n: int, rect: tuple[float, float, float, float], image_path: str | Path
    ) -> None:
        """Place an image file into ``rect`` on page ``n`` (additive)."""
        imageedit.insert_image(self._doc, n, rect, image_path)

    def replace_image(self, n: int, target: ImageInfo, image_path: str | Path) -> None:
        """Swap the image at ``target`` for a new file, same rectangle."""
        imageedit.replace_image(self._doc, n, target, image_path)

    def delete_image(self, n: int, target: ImageInfo) -> None:
        """Remove the image at ``target`` on page ``n``."""
        imageedit.delete_image(self._doc, n, target)

    def move_image(self, n: int, target: ImageInfo, offset: tuple[float, float]) -> None:
        """Move the image at ``target`` by ``offset`` (unrotated page points)."""
        imageedit.move_image(self._doc, n, target, offset)

    def resize_image(
        self, n: int, target: ImageInfo, new_rect: tuple[float, float, float, float]
    ) -> None:
        """Resize the image at ``target`` to ``new_rect`` (unrotated page points)."""
        imageedit.resize_image(self._doc, n, target, new_rect)

    def rotate_image(self, n: int, target: ImageInfo, deg: int) -> None:
        """Rotate the image at ``target`` by ±90° about its rect centre."""
        imageedit.rotate_image(self._doc, n, target, deg)

    # --- OCR (O-series) ---------------------------------------------------
    def ocr_words(self, n: int, *, dpi: int = ocr.DEFAULT_DPI) -> list[OcrWord]:
        """OCR word boxes for page ``n`` (read/extract path only).

        For pages with no text layer (scans, text exported as outlines).
        Boxes are unrotated page points. Raises
        :class:`~pdfcore.ocr.TesseractNotFound` when tesseract is missing.
        """
        return ocr.extract_words(self._doc, n, dpi=dpi)

    def ocr_invoice(self, n: int, *, dpi: int = ocr.DEFAULT_DPI) -> InvoiceExtract:
        """OCR page ``n`` and parse invoice fields from the word boxes (O3).

        Missing fields come back None with warnings — nothing raises on
        absent content; reconciliation (O4) decides what needs a human.
        """
        return invoice.extract_invoice(ocr.extract_words(self._doc, n, dpi=dpi))

    # --- text sourcing (X0: search + Extract Text routing) -----------------
    def has_text_layer(self, n: int) -> bool:
        """True when page ``n`` has extractable native text.

        Drives the native-vs-OCR routing for search and Extract Text.
        """
        return textsource.has_text_layer(self._doc, n)

    def page_text(self, n: int, *, ocr_words: list[OcrWord] | None = None) -> PageText:
        """Page ``n``'s text: native when a layer exists, else caller's OCR.

        ``ocr_words`` is CALLER-supplied (the UI owns the OCR cache) — this
        method never triggers a slow OCR run itself.
        """
        return textsource.collect_page_text(self._doc, n, ocr_words=ocr_words)

    def text_lines(self, n: int) -> list[list[textsource.Word]]:
        """Page ``n``'s native words grouped into reading-order lines (X3).

        The input to the read-only window/marquee selection helpers in
        :mod:`pdfcore.textselect`. Comment/markup text is excluded, so it is
        never selectable or copyable.
        """
        return textselect.page_lines(self._doc, n)

    def search(self, query: str, include_comments: bool = False) -> list[textsource.SearchHit]:
        """Find ``query`` across every page's native text layer (SR1).

        ALWAYS case-insensitive (by design — no toggle exists). One hit per
        occurrence; rects in unrotated page points, one per line fragment.
        Scanned pages have no text layer — the UI's OCR fallback (SR4)
        searches their cached OCR words through the same matcher.
        ``include_comments`` extends into review-comment text (opt-in, E11).
        """
        return textsource.search_document(self._doc, query, include_comments=include_comments)

    # --- review comments (E11: markup, never content, never prints by default)
    def comments(self, n: int) -> list[CommentInfo]:
        """Review comments on page ``n``."""
        return comments.comments_on_page(self._doc, n)

    def comment_at(self, n: int, px: float, py: float) -> CommentInfo | None:
        """The comment under an unrotated page point, or None."""
        return comments.comment_at(self._doc, n, px, py)

    def add_comment(
        self,
        n: int,
        rect: tuple[float, float, float, float],
        text: str,
        author: str,
        callout_target: tuple[float, float] | None = None,
    ) -> int:
        """Add a review comment (a callout when ``callout_target`` is given)."""
        return comments.add_comment(self._doc, n, rect, text, author, callout_target=callout_target)

    def update_comment_text(self, n: int, xref: int, text: str) -> int:
        """Rewrite a comment's text (re-shrinkwraps; returns the NEW xref)."""
        return comments.update_comment_text(self._doc, n, xref, text)

    def move_comment(self, n: int, xref: int, rect: tuple[float, float, float, float]) -> int:
        """Re-anchor a comment at ``rect``'s top-left (returns the NEW xref)."""
        return comments.move_comment(self._doc, n, xref, rect)

    def move_comment_target(self, n: int, xref: int, target: tuple[float, float]) -> int:
        """Re-point a callout's arrowhead (returns the NEW xref)."""
        return comments.move_comment_target(self._doc, n, xref, target)

    def delete_comment(self, n: int, xref: int) -> None:
        comments.delete_comment(self._doc, n, xref)

    def set_comments_hidden(self, hidden: bool) -> int:
        """Hide/show all comments (the print path's no-print mechanism)."""
        return comments.set_comments_hidden(self._doc, hidden)

    # --- snapshots (Phase 2 undo) ----------------------------------------
    def snapshot(self) -> bytes:
        """Serialized current state — transient, for undo; not archival.

        ``garbage=0`` for speed (snapshots are held in memory, never written to
        disk). Encryption is dropped in the bytes (PyMuPDF default), so a
        restore never needs re-authentication.
        """
        return self._doc.tobytes(garbage=0, deflate=True)

    def restore(self, data: bytes) -> None:
        """Replace the document state from a :meth:`snapshot`.

        The source path is preserved; the document becomes stream-backed (no
        open handle on the source file), so :meth:`save_in_place` still works.
        """
        new = pymupdf.open(stream=data, filetype="pdf")
        self._doc.close()
        self._doc = new

    # --- lifecycle ------------------------------------------------------
    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> PdfDocument:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _same_path(a: Path, b: Path) -> bool:
    """Case-insensitive-safe path equality (Windows filesystems are case-fold)."""
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
