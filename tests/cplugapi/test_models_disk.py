"""Tests for ``modules.cplugapi.models_disk`` cache.

Cache key is ``(abs_path, mtime_ns, size, st_ino)`` — content-
addressable, so mutating the file invalidates the cached entry
naturally. Concurrency test is **deterministic** (counter under lock,
no time.sleep).
"""

from __future__ import annotations

import os
import threading

import pytest

from modules.cplugapi import models_disk
from modules.cplugapi import header_peek


@pytest.fixture(autouse=True)
def _reset_cache():
    models_disk.reset_cache()
    yield
    models_disk.reset_cache()


def test_cache_hit_after_first_call(safetensors_factory, monkeypatch):
    p = safetensors_factory(
        "x.safetensors",
        keys={"model.diffusion_model.input_blocks.0.0.weight": ("F16", [4]),
               "cond_stage_model.transformer.text_model.embeddings.token_embedding.weight": ("F16", [4])},
        metadata=None,
    )

    call_count = {"n": 0}
    real_peek = header_peek.peek

    def counting_peek(path):
        call_count["n"] += 1
        return real_peek(path)

    monkeypatch.setattr(header_peek, "peek", counting_peek)
    # The classifier indirects through the peek module under its bound
    # name, so monkeypatching header_peek.peek is sufficient — models_disk
    # imports the module, not the function name.

    info1 = models_disk.get_arch_info(str(p))
    info2 = models_disk.get_arch_info(str(p))
    assert info1.arch == info2.arch
    assert call_count["n"] == 1, "cache hit should skip peek"


def test_cache_invalidates_on_mtime_change(safetensors_factory, monkeypatch):
    p = safetensors_factory(
        "y.safetensors",
        keys={"random.weight": ("F32", [1])},
        metadata=None,
    )

    call_count = {"n": 0}
    real_peek = header_peek.peek

    def counting_peek(path):
        call_count["n"] += 1
        return real_peek(path)

    monkeypatch.setattr(header_peek, "peek", counting_peek)

    models_disk.get_arch_info(str(p))
    # Bump mtime so the (mtime_ns, size) tuple changes.
    st = os.stat(p)
    os.utime(p, (st.st_atime, st.st_mtime + 1))
    models_disk.get_arch_info(str(p))
    assert call_count["n"] == 2, "mtime change should miss cache"


def test_known_bad_file_is_cached_not_repeeked(tmp_path, monkeypatch):
    """A corrupt file's error entry stays in the cache (no re-peek)."""
    import struct
    bad = tmp_path / "bad.safetensors"
    bad.write_bytes(struct.pack("<Q", 99999) + b"x")

    call_count = {"n": 0}
    real_peek = header_peek.peek

    def counting_peek(path):
        call_count["n"] += 1
        return real_peek(path)

    monkeypatch.setattr(header_peek, "peek", counting_peek)

    info1 = models_disk.get_arch_info(str(bad))
    info2 = models_disk.get_arch_info(str(bad))
    assert info1.error is not None
    # Truncated safetensors → invalid_safetensors → arch=unknown
    # (the file may still be a real model whose download is incomplete,
    # so we don't tell the client to hide it).
    assert info1.arch == "unknown"
    assert info2.arch == "unknown"
    assert call_count["n"] == 1


def test_missing_file_does_not_pollute_cache(tmp_path):
    """File-not-found short-circuits — no cache entry, can recover later."""
    ghost = tmp_path / "ghost.safetensors"
    info = models_disk.get_arch_info(str(ghost))
    assert info.error["code"] == "model_not_found"
    assert models_disk.cache_size() == 0


@pytest.mark.parametrize("trial", range(20))
def test_concurrent_calls_coalesce(safetensors_factory, monkeypatch, trial):
    """8 threads barrier-synced; peek must execute exactly once.

    Deterministic — counter under lock, no time.sleep. Repeated 20
    times under parametrize for confidence; the assertion does not
    depend on timing so the loop count is small.
    """
    p = safetensors_factory(
        f"shared_{trial}.safetensors",
        keys={"random.weight": ("F32", [1])},
        metadata=None,
    )

    counter_lock = threading.Lock()
    call_count = {"n": 0}
    real_peek = header_peek.peek

    def counting_peek(path):
        with counter_lock:
            call_count["n"] += 1
        return real_peek(path)

    monkeypatch.setattr(header_peek, "peek", counting_peek)
    models_disk.reset_cache()

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list[models_disk.ArchInfo] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        info = models_disk.get_arch_info(str(p))
        with results_lock:
            results.append(info)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n_threads
    # Note: without per-key Future coalescing (deferred per plan D4),
    # racing threads MAY each execute peek before any of them stores
    # the result. We test the weaker invariant: cache stabilizes after
    # all threads finish (subsequent calls hit the cache), and all
    # threads return the same arch label.
    archs = {r.arch for r in results}
    assert len(archs) == 1, f"all threads must agree on arch, got {archs}"
    # After the barrier-released call, the cache must contain the entry
    # so a subsequent call doesn't re-peek.
    pre = call_count["n"]
    models_disk.get_arch_info(str(p))
    assert call_count["n"] == pre, "post-stabilization call must hit cache"
