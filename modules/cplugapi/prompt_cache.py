"""LRU cache for text-encoder conditioning (audit 01 §4.1).

Upstream `StableDiffusionProcessing.cached_c` / `cached_uc` are single-slot
caches: any change to the params tuple (CFG, emphasis, CLIP_skip, …) blows
them away and re-runs CLIP/T5. For the live-sketching workload — where the
prompt rarely changes but other params toggle — this is the dominant
per-gen cost (~22-35 ms SDXL, ~800 ms cold Flux).

This module backs that single-slot cache with a process-wide LRU. The
existing single-slot cache stays as the fast path; on a miss we consult
the LRU before falling through to text-encoder forward. On compute, both
are populated.

The LRU is invalidated whenever ``clear()`` is called (typically from
``StableDiffusionProcessing.clear_prompt_cache``), so a model swap or any
user-initiated reset still empties the cache.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

# Total slots across both directions (c + uc share the same budget).
# Each slot stores a small conditioning struct; 32 entries is well under
# 1 MB even for SDXL multi-cond, but easily covers an artist toggling
# between several emphasis/CFG combinations on the same prompt.
_MAX_SLOTS = 32

_lock = threading.Lock()
_slots: "OrderedDict[Any, tuple[Any, dict | None]]" = OrderedDict()


def _freeze(value: Any) -> Any:
    """Recursively convert ``value`` into a hashable form.

    ``cached_params`` from ``StableDiffusionProcessing`` contains an
    ``SdConditioning`` (a ``list`` subclass) and an ``extra_network_data``
    dict — neither is hashable, so the raw tuple cannot be used as a dict
    key. Upstream's single-slot cache only compares with ``==``, so it
    never had to care; the LRU does.
    """
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, list):
        return ("__list__", tuple(_freeze(v) for v in value))
    if isinstance(value, dict):
        return ("__dict__", tuple((k, _freeze(value[k])) for k in sorted(value, key=repr)))
    if isinstance(value, set):
        return ("__set__", tuple(sorted((_freeze(v) for v in value), key=repr)))
    try:
        hash(value)
    except TypeError:
        return ("__repr__", repr(value))
    return value


def get(key: Any) -> tuple[Any, dict | None] | None:
    """Return ``(conditioning, extra_params)`` for ``key`` or ``None``.

    ``extra_params`` is the ``last_extra_generation_params`` snapshot
    captured at compute time (mirrors the ``cache[2]`` slot in the
    upstream cache structure).
    """
    frozen = _freeze(key)
    with _lock:
        if frozen in _slots:
            value = _slots.pop(frozen)
            _slots[frozen] = value
            return value
        return None


def put(key: Any, conditioning: Any, extra_params: dict | None) -> None:
    frozen = _freeze(key)
    with _lock:
        if frozen in _slots:
            _slots.pop(frozen)
        _slots[frozen] = (conditioning, extra_params)
        while len(_slots) > _MAX_SLOTS:
            _slots.popitem(last=False)


def clear() -> None:
    with _lock:
        _slots.clear()


def size() -> int:
    with _lock:
        return len(_slots)
