"""Recent-files list — the backing store for the File → Open Recent fly-out.

Qt-free by design (mirrors ``portable.py`` / ``resources.py``): imports only
stdlib, so the ordering / dedup / cap logic is trivially unit-testable without a
QApplication. Persistence is a small JSON array of absolute path strings, most
recent first, written into ``portable.data_dir()`` so the list survives across
launches of the INSTALLED app (and travels with the portable ZIP).

Matching is case-insensitive via ``os.path.normcase`` (Windows paths differ only
in case for the same file), but the ORIGINAL casing is kept for display. A
missing or corrupt store degrades to an empty list — a recent-files list is
never load-bearing, so it must never be the reason a launch fails.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_ENTRIES = 10


class RecentFiles:
    """An ordered, deduplicated, capped list of recently opened files.

    Every mutation persists immediately, so the on-disk store and the in-memory
    list can never drift. All public paths are absolute.
    """

    def __init__(self, store_path: Path) -> None:
        self._store = Path(store_path)
        self._entries: list[str] = self._load()

    # --- persistence ----------------------------------------------------
    def _load(self) -> list[str]:
        try:
            raw = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # missing or corrupt — start empty, never raise
        if not isinstance(raw, list):
            return []
        # Keep strings only, cap the length, and drop any that dedup to an
        # earlier entry (defends against a hand-edited / older-format file).
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            key = os.path.normcase(item)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(item)
            if len(cleaned) >= MAX_ENTRIES:
                break
        return cleaned

    def _save(self) -> None:
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(self._store.suffix + ".tmp")
            tmp.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")
            os.replace(tmp, self._store)
        except OSError:
            pass  # a recent-files list must never break the app on a write fault

    # --- queries --------------------------------------------------------
    def entries(self) -> list[Path]:
        """The recent files, most recent first (absolute paths)."""
        return [Path(p) for p in self._entries]

    # --- mutations ------------------------------------------------------
    def add(self, file_path: Path) -> None:
        """Record ``file_path`` as the most-recent entry.

        Moves an existing entry to the front (case-insensitive match), caps the
        list at ``MAX_ENTRIES``, and persists.
        """
        absolute = os.path.abspath(str(file_path))
        key = os.path.normcase(absolute)
        self._entries = [p for p in self._entries if os.path.normcase(p) != key]
        self._entries.insert(0, absolute)
        del self._entries[MAX_ENTRIES:]
        self._save()

    def remove(self, file_path: Path) -> None:
        """Drop ``file_path`` from the list (used when a recent file has gone)."""
        key = os.path.normcase(os.path.abspath(str(file_path)))
        kept = [p for p in self._entries if os.path.normcase(p) != key]
        if kept != self._entries:
            self._entries = kept
            self._save()

    def clear(self) -> None:
        """Empty the list."""
        if self._entries:
            self._entries = []
            self._save()
