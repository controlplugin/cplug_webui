# 2026-06-18 — controlnet_cache.clear_cache() + unload hook

**Symptom.** After the upstream-neo merge, `modules/sd_models.py
unload_model_weights()` frees the base model (`del` + `soft_empty_cache` +
`gc.collect`) but leaves the fork's ControlNet patched-UNet cache pinned: each
`_CacheEntry.cached_unet` is a STRONG ref and held cnets carry
`_cplug_cache_held=True` (which makes `ControlNet.cleanup` skip the real
`unload_model` via `_noop_unload`). So cycling checkpoints on a long-running
server kept the old patched UNet + cnet resident until FIFO eviction
(`_MAX_ENTRIES=16`), partially defeating "real VRAM free".

**Root cause.** The cnet cache was designed to survive within a session for
reuse; nothing tied its lifetime to a base-model unload.

**Decision.** Add a public `clear_cache()` and wrap `unload_model_weights` from
the fork side (no core edit):
- `clear_cache()` (under `_cache_lock`): walk each entry's
  `cached_unet.controlnet_linked_list`, clear `_cplug_cache_held` on the held
  cnet (so a later `cleanup` calls the REAL `unload_model`), drop the strong
  `cached_unet` ref, then clear `_cache`. It never calls `unload_model`/swaps
  `_noop_unload`, so it is safe to call outside the cleanup swap window.
- `install_unload_hook()` wraps `modules.sd_models.unload_model_weights`
  (lazy import, `functools.wraps`, original runs first then `clear_cache()` in
  a guarded try/except, return value preserved, idempotent via a flag attr,
  fail-soft if sd_models absent). Wired from `runtime.py` right after
  `controlnet_cache.apply()`.

**Alternatives considered.** Editing `unload_model_weights` directly (rejected:
breaks /sdapi byte-identical invariant + hard-imports cplugapi into core);
`script_callbacks` (no suitable model-unloaded hook). Wrapping keeps the core
file untouched and no-ops when cplugapi isn't mounted.

**Blast radius.** `/sdapi/v1/unload-checkpoint` (which calls
`unload_model_weights`) now also clears the cnet cache — desired (the cached
patched UNet is stale once the model unloads). Core files unchanged.

**Failure modes.** If `clear_cache` ran inside the `_noop_unload` swap window
the later unload would be the no-op — avoided because the hook fires from
`unload_model_weights`, which runs outside any cnet-cleanup swap. Dead weakrefs
/ attribute-refusing objects are guarded so one bad entry can't abort the loop.

**Verification.** `tests/cplugapi/test_controlnet_cache.py` +6 (clear empties
cache, drops refs, clears tags; hook wraps/idempotent/fail-soft). Full
`tests/cplugapi` suite green (565 passed, exit 0).

**Rollback.** Revert this commit; `clear_cache`/`install_unload_hook` and the
one `runtime.py` call are self-contained.
