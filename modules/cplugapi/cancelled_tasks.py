"""Per-task cancellation registry with TTL + max-size eviction.

Late status lookups need to distinguish "task ran to completion" from
"task was cancelled mid-flight". Upstream ``modules/progress.py`` only
keeps the last 16 ``finished_tasks`` and has no concept of cancellation,
so we track it separately.

Thread-safe; safe to call from HTTP-thread context.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

# 10-minute TTL covers worst-case client retry windows; 1024 entries is a
# hard backstop against a runaway producer.
_TTL_SECONDS = 600.0
_MAX_ENTRIES = 1024


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, float]" = OrderedDict()

    def add(self, id_task: str) -> None:
        with self._lock:
            now = time.monotonic()
            # OrderedDict.__setitem__ keeps existing key in place; explicit
            # move_to_end ensures recency-ordered eviction works even on a
            # re-add of an already-present id_task. Since time.monotonic()
            # is non-decreasing, position-by-insertion-order matches order
            # by add timestamp.
            self._entries[id_task] = now
            self._entries.move_to_end(id_task)
            self._evict_locked(now)

    def has(self, id_task: str) -> bool:
        with self._lock:
            # Eviction must run before the membership test so an entry
            # past its TTL is correctly reported absent. The walk is
            # O(expired-at-head); on a healthy registry this is zero.
            self._evict_locked(time.monotonic())
            return id_task in self._entries

    def _evict_locked(self, now: float) -> None:
        cutoff = now - _TTL_SECONDS
        # OrderedDict iter is insertion order (oldest first by add time).
        while self._entries:
            oldest = next(iter(self._entries))
            if self._entries[oldest] < cutoff:
                self._entries.popitem(last=False)
            else:
                break
        while len(self._entries) > _MAX_ENTRIES:
            self._entries.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


_registry = _Registry()
add = _registry.add
has = _registry.has
reset = _registry.reset
size = _registry.size
