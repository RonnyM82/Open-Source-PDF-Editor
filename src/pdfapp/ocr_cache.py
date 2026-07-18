"""Per-page OCR word cache — UI-side, the engine stays stateless (X0).

Mirrors page_geometry.GeometryCache: the document is passed at lookup and
never held; invalidation rides DocumentView.after_command — page-scope
``evict_page``, all-scope/save-as ``clear`` — exactly like the render and
geometry caches (CLAUDE.md rule 8).

The key is the page index ONLY: all UI OCR runs at the engine's default dpi,
so an Extract Text pass pre-warms search and vice versa. If a dpi knob ever
appears, the key must grow to ``(page, dpi)``. Empty word lists ARE cached —
a genuinely blank page must not re-OCR on every search keystroke;
``TesseractNotFound`` propagates to the caller and is never cached.
"""

from __future__ import annotations

from pdfcore.document import PdfDocument
from pdfcore.ocr import OcrWord


class OcrWordCache:
    """Lazy per-page OCR words, shared by search and Extract Text."""

    def __init__(self) -> None:
        self._pages: dict[int, list[OcrWord]] = {}

    def words(self, doc: PdfDocument, n: int) -> list[OcrWord]:
        cached = self._pages.get(n)
        if cached is None:
            cached = doc.ocr_words(n)
            self._pages[n] = cached
        return cached

    def evict_page(self, n: int) -> None:
        self._pages.pop(n, None)

    def clear(self) -> None:
        self._pages.clear()
