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
    links,
    ocr,
    pages,
    protect,
    signing,
    textedit,
    textselect,
    textsource,
)
from pdfcore.boxregistry import BoxRecord
from pdfcore.comments import CommentInfo
from pdfcore.imageedit import ImageInfo
from pdfcore.invoice import InvoiceExtract
from pdfcore.links import WORD_LINK_BLUE, WORD_LINK_BLUE_RGB, LinkInfo
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
        # Auth state (memory-only, never persisted): the last password that
        # authenticated, and MuPDF's auth level (0 none / 2 user / 4 owner /
        # 6 both). Needed so save_in_place/restore can re-authenticate their
        # internal reopens — the engine still never prompts (rule 3).
        self._password: str | None = None
        self._auth_level: int = 0
        # EVERY password that authenticated this session: undo can cross a
        # password-CHANGING save, so a restored snapshot may carry OLDER
        # encryption than the current password opens (adversarial-review
        # finding — restore once bricked the doc after a re-protect).
        self._known_passwords: list[str] = []
        # The protection CHOICE for saves (Acrobat's model: a document
        # property, applied at EVERY save). protect.KEEP = preserve whatever
        # the file has; a ProtectionSpec = apply it; None = strip. The choice
        # PERSISTS across saves and snapshot restores — consuming it after
        # one save let undo-past-that-save silently launder the protection
        # away (restored pre-protection bytes + KEEP = plaintext output).
        self._pending: protect.ProtectionSpec | None | protect._Keep = protect.KEEP

    @classmethod
    def open(cls, path: str | Path, password: str | None = None) -> PdfDocument:
        source = Path(path)
        doc = pymupdf.open(source)
        wrapper = cls(doc, source=source)
        # Authenticate for ANY encrypted flavour — owner-only files have
        # needs_pass False (auto-auth) but the supplied password must still
        # be applied to reach owner level (gating on needs_pass silently
        # discarded it and locked the caller out of a file they had the
        # password for — adversarial-review finding).
        if password is not None and (doc.needs_pass or wrapper.is_protected):
            wrapper.authenticate(password)
        return wrapper

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
        CAUTION: it is FALSE for owner-password-only (permissions-locked)
        files, which auto-authenticate — use :attr:`is_protected` for the
        "does this file carry encryption" question.
        """
        return bool(self._doc.needs_pass)

    def authenticate(self, password: str) -> bool:
        """Try ``password``; return ``True`` on success.

        Records the password + auth level (memory-only) so internal reopens
        (save_in_place, restore) can re-authenticate. CAUTION: MuPDF applies
        the level of the password GIVEN — authenticating with the USER
        password after an owner unlock DOWNGRADES live permissions; use
        :meth:`unlock` for owner-level auth, and nothing else in the app
        should call this after open.
        """
        level = int(self._doc.authenticate(password))
        if level:
            self._password = password
            self._auth_level = level
            self._remember_password(password)
        return bool(level)

    def _remember_password(self, password: str) -> None:
        if password not in self._known_passwords:
            self._known_passwords.append(password)

    def unlock(self, owner_pw: str) -> bool:
        """Authenticate at OWNER level; True lifts permission limits live.

        The open (user) password returns False — and any accidental
        downgrade it caused is undone by re-authenticating with the
        previously recorded password (MuPDF applies the level of whatever
        password it last saw).
        """
        previous = self._password
        level = int(self._doc.authenticate(owner_pw))
        if level in (4, 6):
            self._password = owner_pw
            self._auth_level = level
            self._remember_password(owner_pw)
            return True
        if level and previous is not None:
            self._doc.authenticate(previous)  # undo the live downgrade
        return False

    @property
    def is_protected(self) -> bool:
        """True when the CURRENT bytes carry encryption.

        ``needs_pass`` and ``is_encrypted`` both read False for
        owner-password-only files (MuPDF auto-authenticates them with the
        empty user password) — the metadata encryption entry is the one
        reliable indicator (probe-verified).
        """
        return (self._doc.metadata or {}).get("encryption") is not None

    @property
    def is_owner(self) -> bool:
        """True when authenticated at owner level (or the doc is unrestricted)."""
        if not self.is_protected:
            return True
        return self._auth_level in (4, 6)

    @property
    def permissions(self) -> protect.Permissions:
        """What the current auth level allows (all-allowed for plain docs)."""
        if not self.is_protected:
            return protect.Permissions.from_mask(-1)
        return protect.Permissions.from_mask(int(self._doc.permissions))

    @property
    def auth_password(self) -> str | None:
        """The password currently authenticating this document (memory-only)."""
        return self._password

    def set_protection(self, spec: protect.ProtectionSpec | None) -> None:
        """Pend protection applied at EVERY subsequent save (Acrobat's model).

        ``None`` strips protection at the next save. Deliberately NOT
        undoable (like Acrobat) — the pending choice is wrapper state, not
        document content, and survives snapshot restores.
        """
        self._pending = spec

    @property
    def pending_protection(self) -> protect.ProtectionSpec | None | protect._Keep:
        """The pending choice: a spec, None (strip), or protect.KEEP."""
        return self._pending

    @property
    def reopen_password(self) -> str | None:
        """The password that opens this document's NEXT saved output."""
        pending = self._pending
        if isinstance(pending, protect.ProtectionSpec):
            return pending.owner_pw or pending.user_pw
        if pending is None:  # stripping — the output is plain
            return None
        return self._password  # KEEP: whatever authenticates today

    def _encryption_kwargs(self) -> dict:
        pending = self._pending
        if isinstance(pending, protect.ProtectionSpec):
            return protect.encryption_kwargs(pending)
        if pending is None:
            return {"encryption": pymupdf.PDF_ENCRYPT_NONE}
        # KEEP preserves passwords + permission bits and is a no-op on plain
        # documents (probe-verified) — the safe universal default that fixes
        # the old always-strip behaviour (save's default is ENCRYPT_NONE).
        return {"encryption": pymupdf.PDF_ENCRYPT_KEEP}

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
    ) -> tuple[str, ...]:
        """Insert NEW text at a baseline point on page ``n`` (additive).
        Returns the box's VISUAL line texts (content fingerprint).

        ``align`` justifies multi-line text against its widest line.
        """
        return textedit.insert_new_text(self._doc, n, point, text, style=style, align=align)

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

    def insert_blank(self, at: int, width: float, height: float) -> None:
        """Insert a blank page with dimensions in PDF points at ``at``."""
        pages.insert_blank(self._doc, at, width, height)

    def extract_pages(self, page_nos: Iterable[int], out: str | Path) -> None:
        """Export pages from the current in-memory state to a new PDF."""
        pages.extract(self._doc, page_nos, out)

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
        # Encryption: apply the pending spec / strip / KEEP what exists —
        # PyMuPDF's own default is ENCRYPT_NONE, which silently stripped
        # protection from every save before this (the documented wart).
        self._doc.save(str(dest), garbage=4, deflate=True, **self._encryption_kwargs())

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
        # The written file's password — captured BEFORE writing: the internal
        # reopens below (happy path AND recovery) must authenticate it, and
        # only the engine is present at that moment (the UI never sees these
        # reopens). None when the output is plain.
        new_pw = self.reopen_password
        try:
            self._doc.save(str(tmp), garbage=4, deflate=True, **self._encryption_kwargs())
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
            self._post_save_auth(new_pw)  # the temp bytes are encrypted too
            raise
        self._doc = pymupdf.open(source)
        self._post_save_auth(new_pw)

    def _reauth_internal(self, doc: pymupdf.Document) -> None:
        """Re-authenticate an internally (re)opened document, never prompting.

        Tries the current password first, then every other password seen this
        session — a restored snapshot can carry OLDER encryption than the
        current password opens (undo across a password-changing save). Runs
        for EVERY encrypted flavour: owner-only files auto-auth with
        needs_pass False, but the recorded owner password must still be
        applied or a save/undo silently drops the owner unlock
        (adversarial-review findings — never gate this on needs_pass).
        """
        if not doc.needs_pass and (doc.metadata or {}).get("encryption") is None:
            self._auth_level = 0  # plain bytes — nothing to authenticate
            return
        candidates = [self._password] if self._password is not None else []
        candidates += [p for p in self._known_passwords if p not in candidates]
        for password in candidates:
            level = int(doc.authenticate(password))
            if level:
                self._password = password
                self._auth_level = level
                return
        # No session password opens these bytes (shouldn't happen — we only
        # meet bytes we produced or opened). Owner-only bytes remain usable
        # at their auto-auth level; user-pw bytes stay locked and the level
        # honestly reads none.
        self._auth_level = 0

    def _post_save_auth(self, new_pw: str | None) -> None:
        """Re-authenticate an internal reopen after a save.

        A newly applied spec: authenticate with the password we just WROTE —
        unconditionally, because an owner-only output auto-auths with
        needs_pass False and would otherwise stay at the RESTRICTED level,
        locking the owner out of their own document seconds after protecting
        it (adversarial-review finding). A strip leaves a plain file. KEEP
        re-authenticates the session password(s), so an owner unlock
        survives every save. The pending CHOICE is deliberately NOT consumed
        (see __init__ — consuming it let undo launder protection away).
        """
        pending = self._pending
        if isinstance(pending, protect.ProtectionSpec):
            if new_pw is not None:
                level = int(self._doc.authenticate(new_pw))
                self._password = new_pw
                self._auth_level = level
                self._remember_password(new_pw)
        elif pending is None:  # stripped — the file is plain now
            self._password = None
            self._auth_level = 0
        else:  # KEEP — restore the session's auth on the reopen
            self._reauth_internal(self._doc)

    def save_signed(
        self,
        path: str | Path,
        signer: signing.Signer,
        *,
        field_name: str = signing.DEFAULT_FIELD_NAME,
        reason: str | None = None,
        location: str | None = None,
        page_index: int = 0,
        rect: tuple[float, float, float, float] | None = None,
        image_path: str | Path | None = None,
    ) -> signing.SignResult:
        """Flatten the current state and write a digitally SIGNED copy to a NEW path.

        TERMINAL: the current state is flattened to final bytes (the same
        ``garbage=4, deflate=True`` options as :meth:`save`) and pyHanko signs
        THOSE bytes as an incremental update — never through PyMuPDF's save
        path. The OPEN document stays UNSIGNED: further edits/saves produce
        unsigned output, and re-saving the signed file with PyMuPDF would
        invalidate the signature. Refuses the currently-open path (like
        :meth:`save`). Encryption is dropped in the flattened bytes — a
        SIGNED COPY IS UNPROTECTED by design (the UI warns; full
        encrypt-then-sign compose is deferred). ``rect``/``image_path``
        place a visible signature; see :func:`pdfcore.signing.sign_pdf_bytes`.
        """
        dest = Path(path)
        if self._source is not None and _same_path(dest, self._source):
            raise ValueError(
                "Cannot sign over the currently-open file; save the signed copy to a new path."
            )
        data = self._doc.tobytes(garbage=4, deflate=True)
        result = signing.sign_pdf_bytes(
            data,
            signer,
            field_name=field_name,
            reason=reason,
            location=location,
            page_index=page_index,
            rect=rect,
            image_path=image_path,
        )
        dest.write_bytes(result.pdf_bytes)
        return result

    def signature_field_names(self) -> list[str]:
        """Names of all signature form fields — filled AND empty placeholders
        (auto-naming input only; use :meth:`has_signatures` for "is signed")."""
        return signing.signature_field_names(self._doc)

    def has_signatures(self) -> bool:
        """True when any signature field actually HOLDS a signature (an empty
        placeholder field is NOT a signature)."""
        return signing.has_signatures(self._doc)

    def strip_signatures(self) -> int:
        """Remove every signature field (stamps included); returns the count.

        Used by the save flow with the user's consent — a rewrite breaks
        signatures anyway, and a stripped file is honest where a
        broken-signature file reads as tampered.
        """
        return signing.strip_signatures(self._doc)

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

    def set_list_style(
        self,
        n: int,
        para: Paragraph,
        kind: str | None,
        *,
        ordinal: int = 1,
    ) -> textedit.ParagraphReplaceResult:
        """Convert a paragraph to/from a bulleted/numbered list item (L2).

        ``kind``: ``"bullet"`` | ``"number"`` | ``None`` (clear). See
        :func:`textedit.set_list_style`.
        """
        return textedit.set_list_style(self._doc, n, para, kind, ordinal=ordinal)

    def style_paragraph_selection(
        self,
        n: int,
        para: Paragraph,
        line_rects: list[tuple[float, float, float, float]],
        *,
        color: int = WORD_LINK_BLUE,
        underline: bool = True,
    ) -> textedit.ParagraphReplaceResult:
        """Recolour + optionally underline the words of ``para`` covered by
        ``line_rects`` (per-line selection rects), leaving the rest untouched.
        Used to give linked text the classic blue-underline look; the caller
        restricts it to editable (non-embedded, unrotated) paragraphs."""
        return textedit.style_paragraph_selection(
            self._doc, n, para, line_rects, color=color, underline=underline
        )

    def insert_runs(
        self,
        n: int,
        point: tuple[float, float],
        runs: list[StyledRun],
        *,
        align: str = "left",
        pitch: float | None = None,
    ) -> tuple[str, ...]:
        """Insert NEW rich text at a baseline point on page ``n`` (E9). Returns
        the box's VISUAL line texts (content fingerprint).

        ``pitch`` overrides the default 1.2-em baseline spacing (a copy of an
        existing paragraph reproduces that paragraph's own pitch).
        """
        return textedit.insert_new_runs(self._doc, n, point, runs, align=align, pitch=pitch)

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

    def add_box(self, n: int, rect: tuple[float, float, float, float], text: str = "") -> BoxRecord:
        """Register a newly inserted box on page ``n``; returns its record.

        ``text`` is the box's content fingerprint (used to disambiguate
        overlapping boxes — task 5); empty falls back to pure geometry.
        """
        return boxregistry.add_box(self._doc, n, rect, text)

    def update_box_rect(self, box_id: str, rect: tuple[float, float, float, float]) -> None:
        """Record a registered box's new placement after a MOVE/resize (content
        unchanged, so the fingerprint is preserved)."""
        boxregistry.update_box_rect(self._doc, box_id, rect)

    def update_box(self, box_id: str, rect: tuple[float, float, float, float], text: str) -> None:
        """Record a box's new placement AND content after an EDIT changed its
        text (the fingerprint must follow the new content)."""
        boxregistry.update_box(self._doc, box_id, rect, text)

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

    # --- hyperlinks (link annotations) -------------------------------------
    def links(self, n: int) -> list[LinkInfo]:
        """Every link on page ``n`` (unrotated page coordinates)."""
        return links.links_on_page(self._doc, n)

    def link_at(self, n: int, px: float, py: float) -> LinkInfo | None:
        """The link under an unrotated page point on page ``n``, or None."""
        return links.link_at(self._doc, n, px, py)

    def add_link(
        self,
        n: int,
        rect: tuple[float, float, float, float],
        *,
        uri: str | None = None,
        dest_page: int | None = None,
        dest_point: tuple[float, float] | None = None,
    ) -> int:
        """Create a link over ``rect``. Exactly one of ``uri`` (web/email) or
        ``dest_page`` (go-to-page) must be given. Returns the new link's xref."""
        return links.add_link(
            self._doc, n, rect, uri=uri, dest_page=dest_page, dest_point=dest_point
        )

    def update_link(
        self,
        n: int,
        xref: int,
        *,
        uri: str | None = None,
        dest_page: int | None = None,
        dest_point: tuple[float, float] | None = None,
    ) -> None:
        """Change where a link points (rect unchanged). URI ↔ go-to-page allowed."""
        links.update_link(self._doc, n, xref, uri=uri, dest_page=dest_page, dest_point=dest_point)

    def move_link(self, n: int, xref: int, offset: tuple[float, float]) -> None:
        """Translate a link's rectangle by ``offset`` (unrotated page points)."""
        links.move_link(self._doc, n, xref, offset)

    def resize_link(self, n: int, xref: int, new_rect: tuple[float, float, float, float]) -> None:
        """Resize a link's rectangle to ``new_rect`` (unrotated page points)."""
        links.resize_link(self._doc, n, xref, new_rect)

    def delete_link(self, n: int, xref: int) -> None:
        """Remove the link with ``xref`` on page ``n`` (any kind)."""
        links.delete_link(self._doc, n, xref)

    def add_link_rects(
        self,
        n: int,
        rects: list[tuple[float, float, float, float]],
        *,
        uri: str | None = None,
        dest_page: int | None = None,
        dest_point: tuple[float, float] | None = None,
    ) -> list[int]:
        """Create one link per rect (a multi-line text link), all sharing the
        same target. Returns the new links' xrefs."""
        return links.add_link_rects(
            self._doc, n, rects, uri=uri, dest_page=dest_page, dest_point=dest_point
        )

    def underline_rects(
        self,
        n: int,
        rects: list[tuple[float, float, float, float]],
        *,
        color: tuple[float, float, float] = WORD_LINK_BLUE_RGB,
    ) -> None:
        """Draw additive underlines under each rect (the fallback link style for
        text that can't be recoloured in place)."""
        links.underline_rects(self._doc, n, rects, color=color)

    def detect_urls(self, n: int) -> list[links.DetectedUrl]:
        """Every URL/email in page ``n``'s text (normalized target + word rect)."""
        return links.detect_urls(self._doc, n)

    def link_detected_urls(self, n: int, *, style: bool = True) -> int:
        """Turn every URL/email on page ``n`` into a styled hyperlink; returns
        how many were linked."""
        return links.link_detected_urls(self._doc, n, style=style)

    # --- snapshots (Phase 2 undo) ----------------------------------------
    def snapshot(self) -> bytes:
        """Serialized current state — transient, for undo; not archival.

        ``garbage=0`` for speed (snapshots are held in memory, never written
        to disk). Encryption is PRESERVED in the bytes (``PDF_ENCRYPT_KEEP``
        — a no-op on plain docs): the old default silently DROPPED it, so the
        first undo/redo restore left ``KEEP`` saves with nothing to keep and
        every later save wrote plaintext (the milestone's landmine, probe-
        found). :meth:`restore` re-authenticates internally — never a prompt.
        """
        return self._doc.tobytes(garbage=0, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)

    def restore(self, data: bytes) -> None:
        """Replace the document state from a :meth:`snapshot`.

        The source path is preserved; the document becomes stream-backed (no
        open handle on the source file), so :meth:`save_in_place` still works.
        Snapshots of protected documents carry their encryption, so the
        reopen re-authenticates with the recorded password (memory-only);
        the pending protection choice is wrapper state and survives —
        protection is deliberately not undoable, like Acrobat.
        """
        new = pymupdf.open(stream=data, filetype="pdf")
        self._doc.close()
        self._doc = new
        # Multi-password internal re-auth: the snapshot may carry OLDER
        # encryption than the current password (undo across a
        # password-changing save once bricked the tab), and owner-only
        # snapshots need the recorded owner password re-applied even though
        # needs_pass is False (both adversarial-review findings).
        self._reauth_internal(new)

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
