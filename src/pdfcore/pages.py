"""Page-manipulation operations. Pure PyMuPDF, no Qt.

Each in-place op mutates an open ``pymupdf.Document``; ``PdfDocument`` exposes
thin wrappers so the UI has one object to talk to. File-level ops (merge, split)
create new files. Every op is round-trip tested (open -> operate -> save ->
reopen -> assert), per CLAUDE rule 10.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pymupdf

from pdfcore import boxregistry


def rotate(doc: pymupdf.Document, page_nos: Iterable[int], deg: int) -> None:
    """Rotate each page in ``page_nos`` BY ``deg`` degrees (relative).

    ``deg`` must be a multiple of 90 (may be negative). Rotation is normalized to
    ``[0, 360)``.
    """
    if deg % 90 != 0:
        raise ValueError("rotation must be a multiple of 90 degrees")
    for n in page_nos:
        page = doc[n]
        page.set_rotation((page.rotation + deg) % 360)


def delete(doc: pymupdf.Document, page_nos: Iterable[int]) -> None:
    """Delete the given pages. A PDF must keep at least one page.

    Deletes in descending order so earlier indices stay valid as pages are
    removed.
    """
    targets = sorted(set(page_nos), reverse=True)
    if len(targets) >= doc.page_count:
        raise ValueError("cannot delete every page; a PDF must keep at least one page")
    old_count = doc.page_count
    for n in targets:
        doc.delete_page(n)
    # Box registry follows the surviving pages (boxes on deleted pages drop).
    deleted = set(targets)
    mapping: dict[int, int] = {}
    new_index = 0
    for old_index in range(old_count):
        if old_index not in deleted:
            mapping[old_index] = new_index
            new_index += 1
    boxregistry.remap_pages(doc, mapping)


def reorder(doc: pymupdf.Document, order: Iterable[int]) -> None:
    """Reorder pages to ``order``, a permutation of ALL page indices.

    Uses ``Document.select``; every existing page index must appear exactly once
    (``select`` would otherwise drop unlisted pages).
    """
    order = list(order)
    if sorted(order) != list(range(doc.page_count)):
        raise ValueError("order must be a permutation of all page indices")
    # select() rebuilds the catalog and DROPS PieceInfo (probe-verified) —
    # capture the registry first, write it back remapped after.
    boxes_before = boxregistry.read_boxes(doc)
    doc.select(order)
    if boxes_before:
        mapping = {old: new for new, old in enumerate(order)}
        boxregistry.write_boxes(
            doc,
            [
                boxregistry.BoxRecord(b.id, mapping[b.page], b.rect, b.text, b.width)
                for b in boxes_before
                if b.page in mapping
            ],
        )


def insert_from(
    doc: pymupdf.Document,
    src_path: str | Path,
    at: int,
    from_page: int | None = None,
    to_page: int | None = None,
) -> None:
    """Insert pages from another PDF into ``doc``, starting at index ``at``.

    ``from_page``/``to_page`` select an inclusive 0-based source range; omit both
    to insert the whole source. ``at == doc.page_count`` appends.
    """
    old_count = doc.page_count
    src = pymupdf.open(str(src_path))
    try:
        kwargs: dict[str, int] = {"start_at": at}
        if from_page is not None:
            kwargs["from_page"] = from_page
        if to_page is not None:
            kwargs["to_page"] = to_page
        doc.insert_pdf(src, **kwargs)
    finally:
        src.close()
    # Boxes at/after the insertion point shift down by the inserted count.
    inserted = doc.page_count - old_count
    if inserted:
        boxregistry.remap_pages(
            doc, {i: (i + inserted if i >= at else i) for i in range(old_count)}
        )


def insert_blank(doc: pymupdf.Document, at: int, width: float, height: float) -> None:
    """Insert one blank page at ``at`` with dimensions in PDF points."""
    if not 0 <= at <= doc.page_count:
        raise ValueError(f"insertion index {at} is outside this {doc.page_count}-page document")
    if width <= 0 or height <= 0:
        raise ValueError("page width and height must be positive")

    old_count = doc.page_count
    doc.new_page(pno=at, width=width, height=height)
    boxregistry.remap_pages(doc, {i: (i + 1 if i >= at else i) for i in range(old_count)})


def extract(doc: pymupdf.Document, page_nos: Iterable[int], out: str | Path) -> None:
    """Export selected pages from the current in-memory document to a new PDF."""
    selected = list(page_nos)
    if not selected:
        raise ValueError("extract requires at least one page")
    if len(set(selected)) != len(selected):
        raise ValueError("extract page indices must be unique")
    if any(n < 0 or n >= doc.page_count for n in selected):
        raise ValueError("extract page index is outside the document")

    dst = pymupdf.open()
    try:
        for n in selected:
            dst.insert_pdf(doc, from_page=n, to_page=n)

        boxes_before = boxregistry.read_boxes(doc)
        if boxes_before:
            mapping = {old: new for new, old in enumerate(selected)}
            boxregistry.write_boxes(
                dst,
                [
                    boxregistry.BoxRecord(b.id, mapping[b.page], b.rect, b.text, b.width)
                    for b in boxes_before
                    if b.page in mapping
                ],
            )
        dst.save(str(out), garbage=4, deflate=True)
    finally:
        dst.close()


def merge(paths: Iterable[str | Path], out: str | Path) -> None:
    """Concatenate several PDFs into a new file ``out`` (in the given order)."""
    inputs = [Path(p) for p in paths]
    if not inputs:
        raise ValueError("merge requires at least one input file")
    dst = pymupdf.open()
    try:
        for path in inputs:
            src = pymupdf.open(str(path))
            try:
                dst.insert_pdf(src)
            finally:
                src.close()
        dst.save(str(out), garbage=4, deflate=True)
    finally:
        dst.close()


def split(
    path: str | Path,
    ranges: Iterable[tuple[int, int]],
    out_dir: str | Path,
) -> list[Path]:
    """Split ``path`` into one new file per (start, end) range (inclusive, 0-based).

    Files are named ``<stem>_part<N>.pdf`` in ``out_dir`` and the created paths
    are returned in order.
    """
    src_path = Path(path)
    out_path_dir = Path(out_dir)
    range_list = [tuple(r) for r in ranges]
    if not range_list:
        raise ValueError("split requires at least one page range")

    out_path_dir.mkdir(parents=True, exist_ok=True)
    src = pymupdf.open(str(src_path))
    outputs: list[Path] = []
    try:
        n = src.page_count
        for i, (start, end) in enumerate(range_list):
            if not (0 <= start <= end < n):
                raise ValueError(f"invalid range {(start, end)} for a {n}-page document")
            dst = pymupdf.open()
            try:
                dst.insert_pdf(src, from_page=start, to_page=end)
                out_file = out_path_dir / f"{src_path.stem}_part{i + 1}.pdf"
                dst.save(str(out_file), garbage=4, deflate=True)
                outputs.append(out_file)
            finally:
                dst.close()
    finally:
        src.close()
    return outputs
