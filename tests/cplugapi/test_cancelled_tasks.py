"""Unit tests for ``modules.cplugapi.cancelled_tasks``."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from modules.cplugapi import cancelled_tasks


def test_add_then_has(clean_cancelled):
    cancelled_tasks.add("task-A")
    assert cancelled_tasks.has("task-A")
    assert not cancelled_tasks.has("task-B")


def test_re_add_resets_recency(clean_cancelled):
    """Re-adding an existing entry should refresh its position so the
    LRU eviction doesn't drop it before genuinely older entries."""
    cancelled_tasks.add("old")
    time.sleep(0.001)
    cancelled_tasks.add("new")
    cancelled_tasks.add("old")  # bump
    # Both still present.
    assert cancelled_tasks.has("old")
    assert cancelled_tasks.has("new")


def test_ttl_eviction(clean_cancelled):
    """An entry older than _TTL_SECONDS must be evicted on next read."""
    fake_time = [1000.0]

    with patch.object(cancelled_tasks.time, "monotonic", side_effect=lambda: fake_time[0]):
        cancelled_tasks.add("old")
        # Jump 11 minutes — past the 10-min TTL.
        fake_time[0] = 1000.0 + 660.0
        assert not cancelled_tasks.has("old")


def test_max_entries_backstop(clean_cancelled):
    """Hard ceiling at _MAX_ENTRIES — oldest entries drop first."""
    overage = 50
    for i in range(cancelled_tasks._MAX_ENTRIES + overage):
        cancelled_tasks.add(f"task-{i:05d}")
    assert cancelled_tasks.size() == cancelled_tasks._MAX_ENTRIES
    # The first `overage` entries should have been evicted.
    for i in range(overage):
        assert not cancelled_tasks.has(f"task-{i:05d}")
    # The most recent entry is still present.
    assert cancelled_tasks.has(f"task-{cancelled_tasks._MAX_ENTRIES + overage - 1:05d}")


def test_concurrent_adds_are_safe(clean_cancelled):
    """Many threads add disjoint entries; nothing is lost or duplicated."""

    def worker(start):
        for i in range(50):
            cancelled_tasks.add(f"t-{start}-{i}")

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 8 workers × 50 adds = 400 distinct keys (well under MAX_ENTRIES).
    assert cancelled_tasks.size() == 400


def test_reset_clears_all(clean_cancelled):
    for i in range(10):
        cancelled_tasks.add(f"task-{i}")
    cancelled_tasks.reset()
    assert cancelled_tasks.size() == 0


def test_has_returns_true_for_within_ttl_entries(clean_cancelled):
    """An entry within TTL is reported present even when newer siblings
    have been added on top."""
    cancelled_tasks.add("first")
    cancelled_tasks.add("second")
    assert cancelled_tasks.has("first")
    assert cancelled_tasks.has("second")
    assert cancelled_tasks.size() == 2
