"""Resolve bundled resource paths in both dev and PyInstaller-frozen runs.

PyInstaller unpacks files listed in the spec's ``datas`` under a temp directory
exposed as ``sys._MEIPASS``. A bare relative path happens to work when running
from source but silently resolves to the wrong place (or nothing) in the frozen
``.exe`` — so route every bundled-resource lookup through :func:`resource_path`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Dev layout: this file is ``src/pdfapp/resources.py``, so the project root —
# which holds ``assets/`` — is three parents up.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resource_path(relative: str) -> Path:
    """Absolute path to a bundled resource, given its path relative to the
    project root (e.g. ``"assets/icon.png"``).

    Frozen: ``<sys._MEIPASS>/<relative>`` (the spec maps ``assets/`` there).
    Dev:    ``<project-root>/<relative>``.
    """
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base is not None else _PROJECT_ROOT
    return root / relative
