"""Durable identity for user-inserted text boxes (E10). Pure PyMuPDF, no Qt.

The registry lives INSIDE the document: ``/PieceInfo/PDFEditor/Private`` on
the catalog — the page-piece dictionary (ISO 32000 §14.5), the standard's
sanctioned channel for private product data (how Illustrator/InDesign persist
their own editing state in a PDF). Conforming readers ignore it, so rendering
and printing are identical everywhere; a sanitizing processor may legally
strip it, which degrades gracefully (boxes lose identity, content unaffected).

Because undo restores whole-document snapshot BYTES, the registry can never
drift from the content it describes — undo/redo/save/reopen all carry it
automatically (probe-verified through ``tobytes()`` and ``garbage=4`` saves).
This replaces the session-only UI tracking (E9.9), whose evaporation on every
undo/refresh was why inserted boxes kept re-merging with neighbours.

Malformed or missing private data is treated as an empty registry — never an
error (the file may have been made or mangled elsewhere).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pymupdf

_PRIVATE_KEY = "PieceInfo/PDFEditor/Private"


@dataclass(frozen=True)
class BoxRecord:
    """One user-inserted text box: durable id, placement, and a content
    FINGERPRINT (its text, lines joined with "\\n").

    The fingerprint disambiguates OVERLAPPING boxes (task 5 Level 1): when two
    registry rects overlap, a line is assigned to the box whose content it
    matches, not merely the first/nearest rect — so a box moved over other
    text no longer absorbs it. Empty ``text`` (legacy records, or a box whose
    content wasn't supplied) falls back to pure geometry, preserving old
    behaviour.
    """

    id: str
    page: int
    rect: tuple[float, float, float, float]
    text: str = ""


def read_boxes(doc: pymupdf.Document) -> list[BoxRecord]:
    """All registered boxes, or [] when absent/malformed (never raises)."""
    try:
        kind, value = doc.xref_get_key(doc.pdf_catalog(), _PRIVATE_KEY)
    except Exception:
        return []
    if kind != "string" or not value:
        return []
    try:
        entries = json.loads(value)
        return [
            BoxRecord(
                id=str(e["id"]),
                page=int(e["page"]),
                rect=tuple(float(v) for v in e["rect"]),
                text=str(e.get("text", "")),  # absent on pre-fingerprint records
            )
            for e in entries
            if isinstance(e, dict) and len(e.get("rect", ())) == 4
        ]
    except (ValueError, TypeError, KeyError):
        return []  # foreign/mangled private data — treat as no registry


def write_boxes(doc: pymupdf.Document, boxes: list[BoxRecord]) -> None:
    """Replace the registry with ``boxes`` (writes the PieceInfo entry)."""
    payload = json.dumps(
        [{"id": b.id, "page": b.page, "rect": list(b.rect), "text": b.text} for b in boxes]
    )
    doc.xref_set_key(doc.pdf_catalog(), _PRIVATE_KEY, pymupdf.get_pdf_str(payload))


def add_box(
    doc: pymupdf.Document,
    page: int,
    rect: tuple[float, float, float, float],
    text: str = "",
) -> BoxRecord:
    """Register a new box; returns its record (fresh unique id)."""
    box = BoxRecord(id=uuid.uuid4().hex[:12], page=page, rect=tuple(rect), text=text)
    write_boxes(doc, [*read_boxes(doc), box])
    return box


def update_box_rect(
    doc: pymupdf.Document, box_id: str, rect: tuple[float, float, float, float]
) -> None:
    """Move/resize a registered box, PRESERVING its content fingerprint (a move
    doesn't change the text). Unknown ids are ignored."""
    rect = tuple(rect)
    write_boxes(
        doc,
        [BoxRecord(b.id, b.page, rect, b.text) if b.id == box_id else b for b in read_boxes(doc)],
    )


def update_box(
    doc: pymupdf.Document,
    box_id: str,
    rect: tuple[float, float, float, float],
    text: str,
) -> None:
    """Update a box's rect AND content fingerprint (an EDIT changed the text).
    Unknown ids are ignored."""
    rect = tuple(rect)
    write_boxes(
        doc,
        [BoxRecord(b.id, b.page, rect, text) if b.id == box_id else b for b in read_boxes(doc)],
    )


def remove_box(doc: pymupdf.Document, box_id: str) -> None:
    """Drop a box from the registry (unknown ids are ignored)."""
    write_boxes(doc, [b for b in read_boxes(doc) if b.id != box_id])


def remap_pages(doc: pymupdf.Document, mapping: dict[int, int]) -> None:
    """Renumber box pages after a structural op; unmapped pages are dropped.

    Page ops (delete/reorder/insert) change page indices; each op builds the
    old→new mapping and calls this so the registry follows. No-op when the
    registry is empty (avoids creating PieceInfo on untouched documents).
    """
    boxes = read_boxes(doc)
    if not boxes:
        return
    write_boxes(
        doc,
        [BoxRecord(b.id, mapping[b.page], b.rect, b.text) for b in boxes if b.page in mapping],
    )
