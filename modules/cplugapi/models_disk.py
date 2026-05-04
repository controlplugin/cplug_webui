"""LRU-cached arch detection for on-disk checkpoints.

The classifier (``arch.py``) and header peek (``header_peek.py``) are
both pure / I/O-bounded but fast — typical safetensors header read is
~ms. For the ``/cplugapi/v1/models/sd-checkpoints`` endpoint that means
a sub-second cold scan of a typical models dir; once cached, subsequent
calls are dict lookups.

Cache key is **content-addressable**: ``(abs_path, mtime_ns, size)``.
No TTL — when the file changes, the key changes, the entry is naturally
invalidated. Saves a layer of failure modes (clock-skew sensitive TTLs,
"why is my new model not showing up" support burden).

Design mirrors :mod:`idempotency`'s ``_LruCache``: locked
``OrderedDict`` with ``move_to_end`` on read, eviction by count cap.
Cap is configurable via ``CPLUG_MODELS_CACHE_MAX`` (default 4096).

**Race note**: if a file is being written (download in progress),
each peek attempt sees a different ``(mtime, size)`` key → cache miss
→ re-peek. Acceptable: peeks are bounded and self-consistent (header
finishes writing before tensor data, so a peeked header is never
torn relative to the tensor data we're not reading).

**Migration trigger**: if ops feedback indicates cold-scan latency is
user-visible OR the WARNING log lines show high-frequency per-file
errors at scale, rewrite this file's I/O path to async + per-key Future
coalescing. ``ThreadPoolExecutor`` does NOT solve coalescing — it's a
rewrite, not an add.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from . import arch as _arch
from . import header_peek

_log = logging.getLogger(__name__)


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def _config_max() -> int:
    return _read_int_env("CPLUG_MODELS_CACHE_MAX", 4096)


@dataclass(frozen=True)
class ArchInfo:
    """Cached classification result for a checkpoint file.

    ``error`` is non-None if the header peek failed; in that case
    ``arch == ARCH_NOT_A_CHECKPOINT`` and ``dtype is None``. Storing
    errors in the cache means a known-bad file isn't re-peeked every
    request.
    """

    arch: str
    dtype: Optional[str]
    error: Optional[dict]


# Cache key dimensions (in order):
#   - abs_path: identifies which file we asked about
#   - mtime_ns: invalidates on edits / re-downloads
#   - size:     catches partial-overwrite cases where mtime stays
#   - st_ino:   defeats path-reuse + same-(mtime,size) collisions
#               (e.g., ``mtime=0`` from a tar extract without preserve-time
#               on two different files of identical length). On Windows
#               ``st_ino`` is the NTFS file-index, also unique per file.
_CacheKey = tuple[str, int, int, int]


class _LruCache:
    """Bounded LRU keyed on (abs_path, mtime_ns, size).

    No TTL — the key includes file identity, so any change to the file
    naturally produces a new key and the old entry ages out via LRU.
    Cap is re-read on each put so an env-var tweak takes effect without
    a process restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[_CacheKey, ArchInfo] = OrderedDict()

    def get(self, key: _CacheKey) -> Optional[ArchInfo]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: _CacheKey, entry: ArchInfo) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            cap = _config_max()
            while len(self._entries) > cap:
                self._entries.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


_cache = _LruCache()


def reset_cache() -> None:
    """Test-only: clear the arch-detection cache."""
    _cache.reset()


def cache_size() -> int:
    """Test-only: current cache occupancy."""
    return _cache.size()


def _peek_and_classify(path: str) -> ArchInfo:
    try:
        peeked = header_peek.peek(path)
    except header_peek.HeaderPeekError as err:
        return ArchInfo(
            arch=_arch.ARCH_NOT_A_CHECKPOINT,
            dtype=None,
            error={"code": err.code, "message": err.message},
        )
    arch_label = _arch.classify_state_keys(peeked.keys, peeked.metadata)
    dtype = _majority_dtype(peeked.dtypes)
    return ArchInfo(arch=arch_label, dtype=dtype, error=None)


def _majority_dtype(dtypes: dict[str, str]) -> Optional[str]:
    """Return the most common non-empty dtype, or None if there is none.

    Surfaces FP8/BF16/F16 deployments without parsing every tensor. We
    don't filter by key shape — the safetensors header already excludes
    metadata-only entries, and a single dominant dtype is what callers
    actually want to display.
    """
    counts: dict[str, int] = {}
    for dt in dtypes.values():
        if dt:
            counts[dt] = counts.get(dt, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def get_arch_info(path: Union[str, Path]) -> ArchInfo:
    """Cached classify-on-read for a single checkpoint file.

    On cache miss: ``os.stat`` (cheap on NTFS / ext4), peek the
    header, classify, store. On cache hit: return the stored
    :class:`ArchInfo` (including error entries — known-bad files are
    not re-peeked).

    File-not-found at ``os.stat`` time short-circuits to a
    ``model_not_found`` error without touching the cache (the file
    might reappear with the same path later).
    """
    p = os.fspath(path)
    try:
        st = os.stat(p)
    except FileNotFoundError as err:
        return ArchInfo(
            arch=_arch.ARCH_NOT_A_CHECKPOINT,
            dtype=None,
            error={"code": "model_not_found", "message": str(err)},
        )
    except PermissionError as err:
        return ArchInfo(
            arch=_arch.ARCH_NOT_A_CHECKPOINT,
            dtype=None,
            error={"code": "permission_denied", "message": str(err)},
        )

    key: _CacheKey = (os.path.abspath(p), st.st_mtime_ns, st.st_size, st.st_ino)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    info = _peek_and_classify(p)
    _cache.put(key, info)
    return info
