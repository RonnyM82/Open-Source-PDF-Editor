"""Pure-logic tests for the settings store (pdfapp.settings). Qt-free."""

from __future__ import annotations

import json

from pdfapp.settings import Settings


def _store(tmp_path):
    return Settings(tmp_path / "settings.json")


def test_missing_store_returns_defaults(tmp_path):
    s = _store(tmp_path)
    assert s.get("theme") is None
    assert s.get("theme", "dark") == "dark"
    assert s.get("thumbnails_visible", True) is True


def test_set_get_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.set("theme", "light")
    s.set("thumbnails_visible", False)
    assert s.get("theme") == "light"
    assert s.get("thumbnails_visible") is False


def test_persists_across_instances(tmp_path):
    path = tmp_path / "settings.json"
    Settings(path).set("theme", "light")
    assert Settings(path).get("theme") == "light"


def test_set_writes_file(tmp_path):
    path = tmp_path / "settings.json"
    Settings(path).set("show_editable_areas", False)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"show_editable_areas": False}


def test_unchanged_set_does_not_rewrite(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("theme", "dark")
    mtime = path.stat().st_mtime_ns
    s.set("theme", "dark")  # same value → no write
    assert path.stat().st_mtime_ns == mtime


def test_corrupt_store_degrades_to_empty(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not valid json", encoding="utf-8")
    s = Settings(path)
    assert s.get("theme", "dark") == "dark"
    # ...and is still usable afterwards.
    s.set("theme", "light")
    assert Settings(path).get("theme") == "light"


def test_non_dict_store_degrades_to_empty(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert Settings(path).get("theme", "dark") == "dark"
