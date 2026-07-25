"""Signature library — stored signing identities the user is authorised to use.

Qt-free by design (mirrors ``recent_files.py`` / ``settings.py``): imports only
stdlib, so it is unit-testable without a QApplication. Persistence is a JSON
array (``signatures.json``) plus store-owned image assets under a sibling
``signatures/`` folder, both written into ``portable.data_dir()`` at UI time so
the library survives across launches (and travels with the portable ZIP —
image paths are stored RELATIVE to the store file for exactly that reason).

Each profile is a person the user is authorised to sign for: a unique name
(case-insensitive), a main signature image, an optional initials image, and an
optional path to that person's own PKCS#12 certificate (None = the app-wide
default certificate, chosen at signing time). Signature/initials images are
COPIED into the asset folder so a profile survives the original file moving or
being deleted — durable storage is the point of the library. The .p12 is
REFERENCED, never copied: private key material is not silently duplicated, and
its location stays the owner's choice. Passwords are NEVER stored; the UI
prompts at signing time.

The profiles only carry engine inputs — initials are placed as decorative
images (``insert_image``) BEFORE signing so the ONE cryptographic signature
covers them, and the main image is the visible skin via
``sign_pdf_bytes(image_path=...)``; see CLAUDE.md "Digital signing".

A missing or corrupt store degrades to an empty library (never load-bearing);
malformed entries are dropped on load. Writes re-read the file first so a
second window's changes (File > New Window shares this file) survive.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

STORE_FILENAME = "signatures.json"
_ASSETS_DIRNAME = "signatures"


def _slug(name: str) -> str:
    """A filesystem-safe folder name derived from a profile name."""
    cleaned = "".join(c if c.isalnum() else "-" for c in name.casefold())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or "profile"


@dataclass(frozen=True)
class SignatureProfile:
    """One stored signing identity — a person the user is authorised to sign for.

    Image paths are absolute (resolved against the store); ``p12_path`` None
    means "use the app-wide default certificate".
    """

    name: str
    signature_image: Path
    initials_image: Path | None
    p12_path: Path | None


class SignatureStore:
    """A named collection of :class:`SignatureProfile` entries, one JSON file.

    Every mutation persists immediately, so the on-disk store and the
    in-memory list can never drift.
    """

    def __init__(self, store_path: Path) -> None:
        self._store = Path(store_path)
        self._entries: list[dict] = self._load()

    # --- persistence ----------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # missing or corrupt — start empty, never raise
        if not isinstance(raw, list):
            return []
        # Keep well-formed entries only and drop any that dedup to an earlier
        # name (defends against a hand-edited / older-format file).
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            sig = item.get("signature_image")
            if not isinstance(name, str) or not name.strip() or not isinstance(sig, str):
                continue
            key = name.strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            initials = item.get("initials_image")
            p12 = item.get("p12_path")
            cleaned.append(
                {
                    "name": name.strip(),
                    "signature_image": sig,
                    "initials_image": initials if isinstance(initials, str) else None,
                    "p12_path": p12 if isinstance(p12, str) else None,
                }
            )
        return cleaned

    def _save(self) -> None:
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            # Per-process temp name so two windows (File > New Window is a second
            # process sharing this file) can't race on one .tmp.
            tmp = self._store.with_name(f"{self._store.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")
            os.replace(tmp, self._store)
        except OSError:
            pass  # the library must never break the app on a disk fault

    # --- entry <-> profile ----------------------------------------------
    def _assets_root(self) -> Path:
        return self._store.parent / _ASSETS_DIRNAME

    def _resolve(self, stored: str | None) -> Path | None:
        if stored is None:
            return None
        p = Path(stored)
        return p if p.is_absolute() else self._store.parent / p

    def _profile(self, entry: dict) -> SignatureProfile:
        return SignatureProfile(
            name=entry["name"],
            signature_image=self._resolve(entry["signature_image"]),
            initials_image=self._resolve(entry["initials_image"]),
            p12_path=Path(entry["p12_path"]) if entry["p12_path"] else None,
        )

    # --- queries --------------------------------------------------------
    def profiles(self) -> list[SignatureProfile]:
        """All stored profiles, in insertion order."""
        return [self._profile(e) for e in self._entries]

    def get(self, name: str) -> SignatureProfile | None:
        """The profile named ``name`` (case-insensitive), or None."""
        key = name.strip().casefold()
        for entry in self._entries:
            if entry["name"].casefold() == key:
                return self._profile(entry)
        return None

    # --- mutations ------------------------------------------------------
    def add(
        self,
        name: str,
        signature_image: str | Path,
        *,
        initials_image: str | Path | None = None,
        p12_path: str | Path | None = None,
    ) -> SignatureProfile:
        """Store a new profile; the images are COPIED into the store's assets.

        Raises ValueError on an empty/duplicate name (case-insensitive) or a
        missing image/certificate file. The originals can be moved or deleted
        afterwards — the profile keeps working from its own copies.
        """
        clean = str(name).strip()
        if not clean:
            raise ValueError("profile name must not be empty")
        sig_src = Path(signature_image)
        if not sig_src.is_file():
            raise ValueError(f"signature image not found: {sig_src}")
        ini_src = Path(initials_image) if initials_image is not None else None
        if ini_src is not None and not ini_src.is_file():
            raise ValueError(f"initials image not found: {ini_src}")
        p12 = Path(p12_path) if p12_path is not None else None
        if p12 is not None and not p12.is_file():
            raise ValueError(f"certificate file not found: {p12}")

        self._entries = self._load()  # merge a concurrent window's changes
        if any(e["name"].casefold() == clean.casefold() for e in self._entries):
            raise ValueError(f"a signature profile named {clean!r} already exists")

        asset_dir = self._new_asset_dir(clean)
        asset_dir.mkdir(parents=True, exist_ok=True)
        sig_copy = asset_dir / f"signature{sig_src.suffix.lower()}"
        shutil.copyfile(sig_src, sig_copy)
        ini_copy: Path | None = None
        if ini_src is not None:
            ini_copy = asset_dir / f"initials{ini_src.suffix.lower()}"
            shutil.copyfile(ini_src, ini_copy)

        entry = {
            "name": clean,
            "signature_image": sig_copy.relative_to(self._store.parent).as_posix(),
            "initials_image": (
                ini_copy.relative_to(self._store.parent).as_posix() if ini_copy else None
            ),
            "p12_path": str(p12.resolve()) if p12 else None,
        }
        self._entries.append(entry)
        self._save()
        return self._profile(entry)

    def remove(self, name: str) -> None:
        """Drop the profile named ``name`` and delete its copied image assets."""
        key = str(name).strip().casefold()
        self._entries = self._load()  # against the latest on-disk state
        removed = [e for e in self._entries if e["name"].casefold() == key]
        if not removed:
            return
        self._entries = [e for e in self._entries if e["name"].casefold() != key]
        self._save()
        root = self._assets_root().resolve()
        kept_dirs = {
            self._resolve(e[field]).parent.resolve()
            for e in self._entries
            for field in ("signature_image", "initials_image")
            if e[field]
        }
        for entry in removed:
            asset_dir = self._resolve(entry["signature_image"]).parent.resolve()
            # Delete only a folder this store owns OUTRIGHT: exactly one level
            # below the assets root — never the root itself (a hand-edited
            # flat path resolves there and rmtree would take every profile's
            # assets with it) — and never one a surviving profile still
            # references (a hand-edited entry can point inside another
            # profile's folder). Anything else is left alone.
            if asset_dir == root or asset_dir.parent != root:
                continue
            if asset_dir in kept_dirs:
                continue
            shutil.rmtree(asset_dir, ignore_errors=True)

    def _new_asset_dir(self, name: str) -> Path:
        root = self._assets_root()
        base = _slug(name)
        candidate = root / base
        counter = 2
        while candidate.exists():
            candidate = root / f"{base}-{counter}"
            counter += 1
        return candidate
