"""Tests for the UI render cache (pdfapp.render_cache). No Qt required."""

from __future__ import annotations

import pytest

from pdfapp.render_cache import RenderCache


def test_put_then_get_roundtrip():
    cache = RenderCache()
    cache.put(("page", 0), "pixmap-0")
    assert cache.get(("page", 0)) == "pixmap-0"
    assert ("page", 0) in cache


def test_missing_key_returns_none():
    assert RenderCache().get("absent") is None


def test_clear_empties_the_cache():
    cache = RenderCache()
    cache.put("a", 1)
    cache.put("b", 2)
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0
    assert cache.get("a") is None


def test_lru_eviction_drops_least_recently_used():
    cache = RenderCache(capacity=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")  # touch 'a' so 'b' is now least-recently-used
    cache.put("c", 3)  # exceeds capacity -> evict 'b'
    assert "a" in cache
    assert "c" in cache
    assert "b" not in cache
    assert len(cache) == 2


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        RenderCache(capacity=0)


def test_cost_budget_evicts_least_recently_used():
    cache = RenderCache(capacity=10, max_cost=100)
    cache.put("a", 1, cost=60)
    cache.put("b", 2, cost=30)
    cache.get("a")  # touch 'a' so 'b' is least-recently-used
    cache.put("c", 3, cost=40)  # total 130 > 100 -> evict 'b'
    assert "a" in cache and "c" in cache
    assert "b" not in cache
    assert cache.total_cost == 100


def test_newest_entry_survives_even_over_budget():
    cache = RenderCache(capacity=10, max_cost=50)
    cache.put("small", 1, cost=10)
    cache.put("huge", 2, cost=500)  # alone exceeds the budget
    assert "huge" in cache  # the page being displayed must stay cached
    assert "small" not in cache


def test_replacing_a_key_updates_total_cost():
    cache = RenderCache(capacity=10, max_cost=1000)
    cache.put("a", 1, cost=100)
    cache.put("a", 2, cost=250)
    assert cache.total_cost == 250
    assert len(cache) == 1


# --- Phase 2 (E4): page-scoped eviction -----------------------------------


def test_evict_page_drops_all_entries_for_that_page():
    cache = RenderCache()
    cache.put((0, "main", 2.0), "main-0", cost=100)
    cache.put((0, "thumb", 16), "thumb-0", cost=10)
    cache.put((1, "main", 2.0), "main-1", cost=100)
    cache.put("not-a-tuple", "misc", cost=5)

    cache.evict_page(0)

    assert (0, "main", 2.0) not in cache
    assert (0, "thumb", 16) not in cache
    assert cache.get((1, "main", 2.0)) == "main-1"  # other pages untouched
    assert cache.get("not-a-tuple") == "misc"  # non-tuple keys untouched
    assert cache.total_cost == 105  # cost bookkeeping stays exact


def test_evict_page_on_absent_page_is_a_noop():
    cache = RenderCache()
    cache.put((0, "main", 1.0), "main-0", cost=7)
    cache.evict_page(3)
    assert cache.total_cost == 7
    assert len(cache) == 1
