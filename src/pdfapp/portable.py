"""Portable-mode detection for the frozen app.

The installer and the portable ZIP ship the *same* PyInstaller onedir bundle;
the only difference is a marker file. When ``pdf-editor.portable`` sits next to
the exe (present in the portable ZIP, absent from the installed build), the app
runs "portable": it writes its own persistent state — the diagnostics log —
into a ``data/`` folder alongside the exe instead of the user profile, so a copy
carried on a USB stick leaves nothing behind on the host.

Qt-free by design (mirrors ``resources.py``): imports only stdlib, so the tiny
detection logic is trivially unit-testable. Note the exe directory is
``Path(sys.executable).parent`` — NOT ``sys._MEIPASS``, which is the transient
unpack dir, not the folder the user extracted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Shipped in the portable ZIP only (package.ps1 drops it in before zipping and
# removes it afterwards, so the installer bundle never carries it).
MARKER_NAME = "pdf-editor.portable"


def is_portable() -> bool:
    """True when this is a frozen build whose exe has the portable marker beside
    it. Dev runs and the installed build have no marker, so they are never
    portable (the installed exe must keep writing to the user profile, never
    next to a Program Files exe)."""
    if not getattr(sys, "frozen", False):
        return False
    return (Path(sys.executable).parent / MARKER_NAME).exists()


def data_dir() -> Path:
    """Where the app writes its own persistent state.

    Portable: ``<exe folder>/data`` — travels with the app, leaves the host
    untouched. Otherwise (installed / dev): ``%LOCALAPPDATA%\\PDF Editor`` (with
    the same TEMP/cwd fallback the diagnostics log has always used) — a per-user,
    always-writable spot that never writes next to a Program Files exe.
    """
    if is_portable():
        return Path(sys.executable).parent / "data"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "PDF Editor"
