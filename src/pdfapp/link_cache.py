"""Per-page hyperlink cache — UI-side, the engine stays stateless.

Mirrors ocr_cache.OcrWordCache / page_geometry.GeometryCache: the document is
passed at lookup and never held (a DocumentView swaps its PdfDocument on a
save-as reopen — a held reference would serve the old file's links).
Invalidation rides DocumentView.after_command — page-scope ``evict_page``,
all-scope/save-as ``clear`` — exactly like the render, geometry and OCR caches
(CLAUDE.md rule 8). Keyed on the page index only.
"""

from __future__ import annotations

from pdfcore.document import PdfDocument
from pdfcore.links import LinkInfo


class LinkCache:
    """Lazy per-page link list (hover follow, reveal chrome, hit-testing)."""

    def __init__(self) -> None:
        self._pages: dict[int, list[LinkInfo]] = {}

    def links(self, doc: PdfDocument, n: int) -> list[LinkInfo]:
        cached = self._pages.get(n)
        if cached is None:
            cached = doc.links(n)
            self._pages[n] = cached
        return cached

    def evict_page(self, n: int) -> None:
        self._pages.pop(n, None)

    def clear(self) -> None:
        self._pages.clear()
