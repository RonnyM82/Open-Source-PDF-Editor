"""Application settings — a tiny persisted key/value store.

Qt-free by design (mirrors ``recent_files.py`` / ``portable.py``): imports only
stdlib, so it is unit-testable without a QApplication. Persistence is a single
flat JSON object written into ``portable.data_dir()`` so preferences survive
across launches of the installed app (and travel with the portable ZIP).

Deliberately a DUMB string/scalar dict — every value must be JSON-serialisable
(str / bool / int / None; window layout rides as a base64 string). All Qt
conversions (QByteArray <-> base64, QColor <-> hex) happen at the MainWindow
call sites, never in here, so the store stays Qt-free.

A missing or corrupt file degrades to an empty store (every reader supplies its
own default), so a deleted / hand-broken / sanitizer-stripped settings file
behaves exactly like a first launch — settings are never load-bearing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_MISSING = object()


class Settings:
    """A flat, immediately-persisted key/value store backed by one JSON file."""

    def __init__(self, store_path: Path) -> None:
        self._store = Path(store_path)
        self._data: dict[str, Any] = self._load()

    # --- persistence ----------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # missing or corrupt — start empty, never raise
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(self._store.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self._store)
        except OSError:
            pass  # a settings write must never break the app on a disk fault

    # --- access ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key`` and persist immediately.

        A no-op (and no disk write) when the value is unchanged — keeps the
        theme-change / toggle callbacks from rewriting the file needlessly.
        """
        if self._data.get(key, _MISSING) == value:
            return
        self._data[key] = value
        self._save()
