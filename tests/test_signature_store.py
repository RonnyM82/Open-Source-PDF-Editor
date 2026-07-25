"""Signature library store: profiles round-trip, copied assets, degradation.

Pure-stdlib module (no Qt, no engine) — tests exercise it directly with
tmp_path stores and dummy image files (the store copies files, it never
decodes them).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdfapp.signature_store import SignatureStore


def _png(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"fake image bytes for " + name.encode())
    return path


@pytest.fixture
def store(tmp_path) -> SignatureStore:
    return SignatureStore(tmp_path / "data" / "signatures.json")


def test_add_roundtrip_with_copied_assets(store, tmp_path):
    sig = _png(tmp_path, "scott-sig.png")
    initials = _png(tmp_path, "scott-initials.png")
    profile = store.add("Scott", sig, initials_image=initials)

    assert profile.name == "Scott"
    assert profile.signature_image.read_bytes() == sig.read_bytes()
    assert profile.initials_image.read_bytes() == initials.read_bytes()
    # The images are store-owned COPIES: the originals can vanish and the
    # profile keeps working.
    assert profile.signature_image != sig
    sig.unlink()
    initials.unlink()
    again = store.get("Scott")
    assert again.signature_image.is_file()
    assert again.initials_image.is_file()

    # Stored paths are RELATIVE to the store file (portable ZIP travels).
    raw = json.loads((tmp_path / "data" / "signatures.json").read_text(encoding="utf-8"))
    assert raw[0]["signature_image"].startswith("signatures/")
    assert not Path(raw[0]["signature_image"]).is_absolute()


def test_persists_across_instances(store, tmp_path):
    store.add("Ronny", _png(tmp_path, "r.png"))
    reopened = SignatureStore(tmp_path / "data" / "signatures.json")
    assert [p.name for p in reopened.profiles()] == ["Ronny"]
    assert reopened.get("ronny").signature_image.is_file()


def test_rejects_duplicates_and_bad_args(store, tmp_path):
    sig = _png(tmp_path, "s.png")
    store.add("Scott", sig)
    with pytest.raises(ValueError, match="already exists"):
        store.add("scott", sig)  # case-insensitive
    with pytest.raises(ValueError, match="must not be empty"):
        store.add("   ", sig)
    with pytest.raises(ValueError, match="signature image not found"):
        store.add("Other", tmp_path / "missing.png")
    with pytest.raises(ValueError, match="initials image not found"):
        store.add("Other", sig, initials_image=tmp_path / "missing.png")
    with pytest.raises(ValueError, match="certificate file not found"):
        store.add("Other", sig, p12_path=tmp_path / "missing.p12")
    assert [p.name for p in store.profiles()] == ["Scott"]


def test_optional_fields_roundtrip_as_none(store, tmp_path):
    store.add("Just Images", _png(tmp_path, "j.png"))
    profile = SignatureStore(store._store).get("Just Images")
    assert profile.initials_image is None
    assert profile.p12_path is None


def test_remove_drops_entry_and_assets(store, tmp_path):
    store.add("Scott", _png(tmp_path, "s.png"), initials_image=_png(tmp_path, "i.png"))
    asset_dir = store.get("Scott").signature_image.parent
    assert asset_dir.is_dir()

    store.remove("SCOTT")
    assert store.get("Scott") is None
    assert not asset_dir.exists()
    # Removing a name that isn't there is a quiet no-op.
    store.remove("Scott")


def test_missing_or_corrupt_store_degrades_to_empty(tmp_path):
    assert SignatureStore(tmp_path / "nowhere.json").profiles() == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json at all", encoding="utf-8")
    assert SignatureStore(bad).profiles() == []
    # Malformed entries are dropped, well-formed ones survive.
    mixed = tmp_path / "mixed.json"
    mixed.write_text(
        json.dumps(["nonsense", {"name": "Kept", "signature_image": "signatures/k/s.png"}]),
        encoding="utf-8",
    )
    assert [p.name for p in SignatureStore(mixed).profiles()] == ["Kept"]


def _append_raw_entry(store_path: Path, entry: dict) -> None:
    """Hand-edit the JSON the way a legacy/older-format file could look."""
    raw = json.loads(store_path.read_text(encoding="utf-8"))
    raw.append(entry)
    store_path.write_text(json.dumps(raw), encoding="utf-8")


def test_remove_legacy_flat_entry_spares_assets_root(store, tmp_path):
    """A hand-edited entry whose image sits DIRECTLY under the assets root must
    not let remove() rmtree the whole root (adversarial-review finding — every
    other profile's copied assets lived there)."""
    store.add("Scott", _png(tmp_path, "s.png"))
    scott_image = store.get("Scott").signature_image
    store_path = tmp_path / "data" / "signatures.json"
    flat = store_path.parent / "signatures" / "old-flat.png"
    flat.write_bytes(b"legacy")
    _append_raw_entry(
        store_path,
        {"name": "Legacy", "signature_image": "signatures/old-flat.png"},
    )

    reopened = SignatureStore(store_path)
    reopened.remove("Legacy")
    assert reopened.get("Legacy") is None
    assert scott_image.is_file(), "removing a legacy entry destroyed another profile's assets"


def test_remove_entry_inside_other_profile_spares_it(store, tmp_path):
    """A hand-edited entry pointing INSIDE another profile's folder must not
    delete that folder on remove()."""
    store.add("Ronny", _png(tmp_path, "r.png"))
    ronny_image = store.get("Ronny").signature_image
    store_path = tmp_path / "data" / "signatures.json"
    rel = f"signatures/{ronny_image.parent.name}/whatever.png"
    _append_raw_entry(store_path, {"name": "Evil", "signature_image": rel})

    reopened = SignatureStore(store_path)
    reopened.remove("Evil")
    assert ronny_image.is_file(), "removing a foreign entry destroyed Ronny's assets"


def test_two_windows_merge_on_add(store, tmp_path):
    """The re-read-before-write pattern: adds from two instances (File > New
    Window shares the store file) must BOTH survive."""
    second = SignatureStore(tmp_path / "data" / "signatures.json")
    store.add("A", _png(tmp_path, "a.png"))
    second.add("B", _png(tmp_path, "b.png"))  # stale in-memory list re-reads

    fresh = SignatureStore(tmp_path / "data" / "signatures.json")
    assert {p.name for p in fresh.profiles()} == {"A", "B"}


def test_p12_is_referenced_not_copied(store, tmp_path):
    p12 = tmp_path / "scott.p12"
    p12.write_bytes(b"fake pkcs12")
    profile = store.add("Scott", _png(tmp_path, "s.png"), p12_path=p12)

    # The private-key bundle stays where the user keeps it — path equality,
    # no copy under the store's asset folder.
    assert profile.p12_path == p12.resolve()
    asset_files = list(profile.signature_image.parent.iterdir())
    assert all(f.suffix != ".p12" for f in asset_files)
