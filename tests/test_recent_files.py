"""Pure-logic tests for the recent-files store (pdfapp.recent_files).

Qt-free — mirrors test_ui_portable: ordering, dedup, the 10-entry cap, and
graceful degradation on a missing / corrupt store, all against a tmp JSON file.
"""

from __future__ import annotations

import json

from pdfapp.recent_files import MAX_ENTRIES, RecentFiles


def _store(tmp_path):
    return RecentFiles(tmp_path / "recent_files.json")


def test_empty_when_no_store(tmp_path):
    assert _store(tmp_path).entries() == []


def test_add_puts_most_recent_first(tmp_path):
    rf = _store(tmp_path)
    rf.add(tmp_path / "a.pdf")
    rf.add(tmp_path / "b.pdf")
    names = [p.name for p in rf.entries()]
    assert names == ["b.pdf", "a.pdf"]


def test_reopen_moves_to_front_without_duplicating(tmp_path):
    rf = _store(tmp_path)
    rf.add(tmp_path / "a.pdf")
    rf.add(tmp_path / "b.pdf")
    rf.add(tmp_path / "a.pdf")  # re-open the older one
    names = [p.name for p in rf.entries()]
    assert names == ["a.pdf", "b.pdf"]


def test_capped_at_max_entries(tmp_path):
    rf = _store(tmp_path)
    for i in range(MAX_ENTRIES + 5):
        rf.add(tmp_path / f"f{i}.pdf")
    entries = rf.entries()
    assert len(entries) == MAX_ENTRIES
    # Oldest ones dropped; newest kept, most-recent first.
    assert entries[0].name == f"f{MAX_ENTRIES + 4}.pdf"


def test_entries_are_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rf = _store(tmp_path)
    rf.add(tmp_path / "sub" / "rel.pdf")
    assert rf.entries()[0].is_absolute()


def test_persists_across_instances(tmp_path):
    store = tmp_path / "recent_files.json"
    RecentFiles(store).add(tmp_path / "a.pdf")
    reloaded = RecentFiles(store)
    assert [p.name for p in reloaded.entries()] == ["a.pdf"]


def test_remove_drops_entry(tmp_path):
    rf = _store(tmp_path)
    rf.add(tmp_path / "a.pdf")
    rf.add(tmp_path / "b.pdf")
    rf.remove(tmp_path / "a.pdf")
    assert [p.name for p in rf.entries()] == ["b.pdf"]


def test_clear_empties(tmp_path):
    rf = _store(tmp_path)
    rf.add(tmp_path / "a.pdf")
    rf.clear()
    assert rf.entries() == []


def test_corrupt_store_degrades_to_empty(tmp_path):
    store = tmp_path / "recent_files.json"
    store.write_text("{ not valid json", encoding="utf-8")
    rf = RecentFiles(store)
    assert rf.entries() == []
    # ...and is still usable afterwards.
    rf.add(tmp_path / "a.pdf")
    assert [p.name for p in rf.entries()] == ["a.pdf"]


def test_non_list_store_degrades_to_empty(tmp_path):
    store = tmp_path / "recent_files.json"
    store.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert RecentFiles(store).entries() == []


def test_concurrent_instances_merge_additions(tmp_path):
    """Two windows sharing one file: each add() re-reads first, so neither
    window's recent additions are lost (read-modify-write)."""
    path = tmp_path / "recent_files.json"
    a = RecentFiles(path)  # window A
    b = RecentFiles(path)  # window B (independent snapshot)
    a.add(tmp_path / "a.pdf")
    b.add(tmp_path / "b.pdf")  # B re-reads, sees a.pdf, adds b.pdf on top
    names = [p.name for p in RecentFiles(path).entries()]
    assert names[0] == "b.pdf"  # most recent
    assert "a.pdf" in names  # A's entry survived
