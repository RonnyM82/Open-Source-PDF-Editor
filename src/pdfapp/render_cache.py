"""A dumb render cache for the UI layer.

Lives in pdfapp, never in pdfcore — the engine render stays stateless. Keyed by
whatever the caller chooses (we use ``(page_index, spec)`` where spec identifies
the render, e.g. the exact render zoom or the thumbnail dpi).

**Clear-on-mutation is the rule that matters** (CLAUDE.md rule 8): every
structural change and every rotate clears the whole cache. A stale render showing
the wrong page is the bug this prevents. The LRU bounds (item count and an
optional byte budget for the pixel buffers) just keep memory sane now that pages
are rendered at display resolution; they are not the point.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable


class RenderCache:
    def __init__(self, capacity: int = 128, max_cost: int | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if max_cost is not None and max_cost < 1:
            raise ValueError("max_cost must be >= 1 (or None for no budget)")
        self._store: OrderedDict[Hashable, tuple[object, int]] = OrderedDict()
        self._capacity = capacity
        self._max_cost = max_cost
        self._total_cost = 0

    def get(self, key: Hashable) -> object | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        self._store.move_to_end(key)
        return entry[0]

    def put(self, key: Hashable, value: object, cost: int = 0) -> None:
        """Insert ``value``. ``cost`` is its approximate size in bytes (e.g.
        ``width * height * 4`` for a pixmap); 0 opts out of the byte budget."""
        old = self._store.pop(key, None)
        if old is not None:
            self._total_cost -= old[1]
        self._store[key] = (value, cost)
        self._total_cost += cost
        self._evict()

    def clear(self) -> None:
        self._store.clear()
        self._total_cost = 0

    def evict_page(self, page_index: int) -> None:
        """Drop every entry whose tuple key starts with ``page_index``.

        Page-scoped invalidation (Phase 2): a content edit changes one page
        without touching count/order, so only that page's ``(n, "main", zoom)``
        and ``(n, "thumb", dpi)`` entries go stale. Structural changes and
        undo/redo restores still use :meth:`clear`.
        """
        stale = [k for k in self._store if isinstance(k, tuple) and k and k[0] == page_index]
        for key in stale:
            _value, cost = self._store.pop(key)
            self._total_cost -= cost

    def _evict(self) -> None:
        # Never evict the newest entry, even if it alone exceeds the budget —
        # the page being displayed must stay cached.
        while len(self._store) > 1 and (
            len(self._store) > self._capacity
            or (self._max_cost is not None and self._total_cost > self._max_cost)
        ):
            _, (_, cost) = self._store.popitem(last=False)
            self._total_cost -= cost

    @property
    def total_cost(self) -> int:
        return self._total_cost

    def __contains__(self, key: Hashable) -> bool:
        return key in self._store

    def __len__(self) -> int:
        return len(self._store)
