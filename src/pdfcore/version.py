"""Single-source app version resolution.

The release version lives ONLY in ``pyproject.toml`` ``[project].version`` —
the same single source ``scripts/_version.ps1`` feeds the installer from.
This module resolves it at runtime in every launch context:

- source checkout: read ``<project-root>/pyproject.toml`` (three parents up —
  the ``pdfapp/resources.py`` convention);
- frozen build: read ``<sys._MEIPASS>/pyproject.toml`` — the spec bundles it
  via ``datas`` exactly so this lookup works;
- installed wheel (no pyproject.toml on disk): ``importlib.metadata``. This is
  deliberately LAST — an editable install's metadata is baked at install time,
  so after a version bump it would be stale until reinstall, while the
  pyproject read is always current.

Falls back to ``"0.0.0"`` — loudly wrong rather than silently stale (the
theme.apply_theme philosophy) — and never raises: version display must not be
able to break an app launch.

Lives in pdfcore (stdlib only, no Qt) so BOTH layers derive ``__version__``
from one implementation: pdfapp may import pdfcore, never the reverse.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FALLBACK = "0.0.0"

# Dev layout: this file is ``src/pdfcore/version.py``, so the project root —
# which holds ``pyproject.toml`` — is three parents up.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def app_version() -> str:
    """The release version from pyproject.toml (dev + frozen), metadata as a
    last resort, ``"0.0.0"`` when nothing is readable. Never raises."""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base is not None else _PROJECT_ROOT
    try:
        import tomllib

        with open(root / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:  # noqa: BLE001 - fall through to the next source
        pass
    try:
        from importlib.metadata import version

        return version("pdf-editor")
    except Exception:  # noqa: BLE001 - degrade loudly, never raise
        return _FALLBACK
