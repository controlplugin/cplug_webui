"""Unit tests for ``modules.cplugapi.prompt_cache`` (audit 01 §4.1)."""

from __future__ import annotations

import pytest

from modules.cplugapi import prompt_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    prompt_cache.clear()
    yield
    prompt_cache.clear()


def test_get_returns_none_for_missing_key():
    assert prompt_cache.get(("foo",)) is None


def test_put_then_get_round_trips():
    key = ("prompt", 20, "model-hash")
    prompt_cache.put(key, "cond-tensor", {"meta": "x"})
    assert prompt_cache.get(key) == ("cond-tensor", {"meta": "x"})


def test_put_overwrites_existing_value():
    key = ("prompt",)
    prompt_cache.put(key, "v1", None)
    prompt_cache.put(key, "v2", {"new": True})
    assert prompt_cache.get(key) == ("v2", {"new": True})


def test_lru_evicts_oldest_when_over_capacity():
    """Filling past the slot budget drops the least-recently-used entry."""
    for i in range(40):
        prompt_cache.put((f"k{i}",), f"v{i}", None)
    # Defaults reserve 32 slots; first 8 should have been evicted.
    assert prompt_cache.size() == 32
    assert prompt_cache.get(("k0",)) is None
    assert prompt_cache.get(("k7",)) is None
    assert prompt_cache.get(("k8",)) == ("v8", None)
    assert prompt_cache.get(("k39",)) == ("v39", None)


def test_get_promotes_to_mru():
    """A get on an old entry should keep it from being the next victim."""
    for i in range(32):
        prompt_cache.put((f"k{i}",), f"v{i}", None)
    # Touch the oldest — it should now be MRU and survive the next put.
    prompt_cache.get(("k0",))
    prompt_cache.put(("k_new",), "vnew", None)
    assert prompt_cache.get(("k0",)) == ("v0", None)
    # k1 was the oldest after the touch and should be the eviction victim.
    assert prompt_cache.get(("k1",)) is None


def test_clear_empties_cache():
    prompt_cache.put(("k",), "v", None)
    prompt_cache.clear()
    assert prompt_cache.size() == 0
    assert prompt_cache.get(("k",)) is None
