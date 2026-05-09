# 2026-05-09 — ControlNet patcher cache (upstream deviation)

**Kind**: code change with non-trivial monkey-patch on upstream Forge code.
**File**: `modules/cplugapi/controlnet_cache.py` (+ `runtime.py` wiring + tests).
**Capability**: `controlnet/patcher-cache`.
**Rollback**: `CPLUG_CONTROLNET_CACHE=0` env var → wrapped function falls through to upstream behaviour, no other side effects.

## Symptom

During live-sketching with ControlNet enabled (Xinsir Union ProMax on
Illustrious-XL, 24 GB card, `--highvram`), every gen produced this
log triplet:

```
Reusing ControlNet Model...
Current ControlNet ControlNetPatcher: ... xinsirUnionProMax_v10.safetensors
ControlNet Method None patched.
Requested to load ControlNet
Requested to load KModel
loaded completely; 14624.38 MB usable, 2396.80 MB loaded, full load: True
Moving model(s) has taken 0.92 seconds
```

The "Moving model(s) has taken 0.92 seconds" line appears every gen.
Across rapid sketch strokes that's a real per-stroke tax — and the
log triplet is misleading anyway: nothing is *actually* being loaded
(the weights stayed resident).

## Root cause

`backend/patcher/controlnet.py:apply_controlnet_advanced` does:

```python
cnet = controlnet.copy().set_cond_hint(image_bchw, strength, ...)
m = unet.clone()                       # NEW UnetPatcher per gen
m.add_patched_controlnet(cnet)
return m
```

`m` is a fresh `UnetPatcher` instance every gen. `LoadedModel.__eq__`
in `backend/memory_management.py:504-505` compares patcher *identity*
(`self.model is other.model`), so the lookup at line 629
(`current_loaded_models.index(loaded_model)`) misses unconditionally.
The miss triggers the clone-cleanup path at lines 642-650 — detach
the old patcher, attach the new one. The underlying weights stay in
VRAM, but the detach/reattach walk costs ~1 s.

This is the same class of bug we already fixed for ToMe in
`modules/sd_models.py:apply_token_merging` (commit `2b06f5f2`).
ControlNet is the second instance.

## Alternatives considered

### Option A — broaden `LoadedModel.__eq__`

First instinct: change equality to compare underlying-model identity
instead of patcher identity. That would fix ToMe, ControlNet, and
any future patcher class in one shot.

**Rejected** after a deeper read of `load_models_gpu`. The
clone-cleanup path is doing essential work, not just bookkeeping:
detaching the old patcher releases its hooks from the underlying
`nn.Module`, then the new patcher's `model_load` re-attaches the
new hooks. With broadened equality:

1. `index()` would match the OLD `LoadedModel` and append it to
   `models_to_load`, discarding the new patcher entirely.
2. The clone-loop would then find the OLD patcher in
   `current_loaded_models` (it's a clone of itself), schedule it for
   unload, and detach it.
3. `model_load` on the OLD patcher reattaches the OLD hooks — with
   the OLD ControlNet `cond_hint`. The new sketch image silently
   disappears from the conditioning.

This is a correctness break worse than the perf bug. Equality
broadening only works in conjunction with a custom patch-transfer
step in `load_models_gpu` (move the new patcher's `cond_hint` /
patches onto the old patcher before reattach), which would be
~30 lines of careful invariant-keeping inside upstream code we don't
own.

### Option B — targeted cache (chosen)

Cache the cloned UNet patcher at the `apply_controlnet_advanced`
boundary. On cache hit, mutate the per-gen state (`cond_hint`,
`strength`, `timestep_percent_range`, the five
`advanced_*_weighting` attrs, `control_type`) directly on the
already-attached `cnet`. Patcher identity is preserved across gens,
so Forge's existing equality logic continues to work as designed —
the lookup at `:629` succeeds, no clone-cleanup fires, no
"Moving model(s)" line gets logged.

### Option C — accept the tax

5% overhead on an 18-second 32-step gen, only visible during rapid
strokes which auto-preempt already collapses to one final gen. Live
with it.

Rejected because the fix is small, the win compounds with future
optimisations, and the misleading log spam alone is operator-hostile.

## Cache design

**Key**: `(id(unet_baseline), id(controlnet_baseline))` — the
BEFORE-clone inputs to `apply_controlnet_advanced`. They change
when:

- LoRA application rebuilds `forge_objects_after_applying_lora.unet`
  → new `id()` for the unet baseline → cache miss → fresh patch.
- Checkpoint or ControlNet model swap → same.
- A second ControlNet stacks on top of the first → key becomes
  `(id(m_with_cn1), id(cn2))` — distinct from the single-CN key.
  Stacked CNs work without special-casing.

**Value**: the cached cloned UNet patcher (`m`) plus weakrefs to
each baseline. The weakrefs guard against `id()` reuse: Python may
recycle the integer id of a GC'd object, so a stale key could
otherwise serve a wrong cached value. Verifying
`weak_baseline() is current_baseline` on lookup catches this.

**On hit**: mutate the existing `cnet` linked into the cached `m`'s
`controlnet_linked_list`. Single-cnet-per-entry contract — we never
call `add_patched_controlnet` again, which would append a duplicate.

**On miss**: invoke the original `apply_controlnet_advanced`. Stash
the result + weakrefs.

**Cap**: 16 entries, FIFO eviction. Realistic usage is 1-3.

## Why monkey-patch from `modules/cplugapi/`

CLAUDE.md hard invariant 2 says all fork code lives in
`modules/cplugapi/`. The pattern is established in
`modules/cplugapi/memmgmt_patches.py` (which guards
`LoadedModel.is_dead` against upstream issue #694). The cache module
mirrors that shape: `apply()` rebinds the symbol on the upstream
module, idempotent via a class-level flag, and falls back to a
silent no-op when the backend isn't importable (test environment).

The ToMe fix landed in-place in `modules/sd_models.py` because that
file already had cplug-specific edits and was easier to amend than
to wrap. ControlNet's wrapper is structurally identical to
`memmgmt_patches`, so it follows that pattern instead — keeps
`backend/patcher/controlnet.py` clean for rebases.

## Blast radius

Touched files:

- `modules/cplugapi/controlnet_cache.py` (new, 350 lines including
  comments).
- `modules/cplugapi/runtime.py` — added bootstrap call after
  `memmgmt_patches`.
- `tests/cplugapi/test_controlnet_cache.py` (new, 12 tests).

Behaviour change scope: only `apply_controlnet_advanced` is wrapped.
Any other `backend/patcher/controlnet.py` symbol is untouched.

## Failure modes

1. **Cache serves stale `cond_hint`** — would manifest as
   ControlNet conditioning the diffusion with the previous sketch
   stroke instead of the current one.
   - Tests:
     `test_cache_hit_mutates_per_gen_state`,
     `test_cache_hit_mutates_advanced_weighting_fields`.
   - Production rollback: `CPLUG_CONTROLNET_CACHE=0` → passthrough.

2. **`id()` reuse after GC** — Python recycles ids; without the
   weakref guard the cache could serve a stale entry under a key
   that now points to a different baseline.
   - Test: `test_stale_entry_is_dropped_when_baseline_gc`.

3. **Upstream renames `apply_controlnet_advanced`** — without
   detection, the wrapper silently fails to install and the perf
   bug returns.
   - Test: `test_apply_logs_warning_when_symbol_missing`. The
     wrapper logs at WARNING when the symbol is absent so the
     mismatch surfaces on the next webui boot.

4. **Multiple parallel gens entering the wrapper** — Forge's
   `queue_lock` serialises gens, but auto-preempt's late-abort hook
   plus future async paths could in principle land here
   concurrently. The wrapper holds a lock around dict mutation;
   concurrent miss for the same key is harmless (both callers patch
   fresh, the second install overwrites, the loser's UNet is GC'd).

## Rebase risk

Low. The wrapped symbol is `apply_controlnet_advanced` which has
been stable on upstream Forge Neo since well before fork. The
function signature change would be detected by the symbol-missing
warning. The `controlnet_linked_list` field on `UnetPatcher` is the
only other internal we touch directly (in the mutate-on-hit path);
that's also stable.

If an upstream change moves the patcher logic into a class method or
restructures the cnet attachment, the wrapper falls back to the
original behaviour (the cache hit branch becomes unreachable when
`controlnet_linked_list` is None) and the perf bug returns silently.
The capability `controlnet/patcher-cache` flips to "absent" on
boot — clients can detect the regression.

## Numbers

Pre-fix per-gen overhead (`Moving model(s) has taken X seconds`
line):

```
Gen 1: 0.98 s
Gen 2: 0.92 s
Gen 3: 0.94 s
Gen 4: 0.96 s
```

Post-fix: line doesn't print at all (sub-100 ms means the upstream
guard at `memory_management.py:691` skips logging). On a 18-second
gen that's ~5% reclaimed; on shorter previews (sketch mode with
ToMe + lower step counts) the relative win is larger.

The "Requested to load KModel" / "Requested to load ControlNet"
log triplet also disappears, because the index lookup at `:629`
now succeeds and the load path doesn't fire its info-level
"requested" announcement.

## Tests run

`pytest tests/` → 332 passed, 4 skipped (unchanged skip count —
torch-dependent `pickle_factory` cases).

## Follow-up — cache key was missing in production

Initial implementation keyed on `(id(unet), id(controlnet))`. The fix
worked in isolation but missed every gen in production: `controlnet`
is a fresh `ControlNet` wrapper each gen because Forge's controlnet
integration layer
(`extensions-builtin/sd_forge_controlnet/scripts/controlnet.py:378`)
calls `try_load_supported_control_model` per gen, which calls
`try_build_from_state_dict` (`modules_forge/supported_controlnet.py:80,203`)
and returns a brand-new wrapper. The underlying `cldm.ControlNet`
weights ARE cached in `_CONTROL_MODEL_CACHE`, but the wrapper around
them isn't.

Symptom in the live log:

```
preempt fired: ...
Reusing ControlNet Model...
ControlNet Method None patched.
Requested to load ControlNet
Requested to load KModel
loaded completely; ... 2396.80 MB loaded, full load: True
Moving model(s) has taken 1.19 seconds
```

The triplet still fired on every subsequent gen even with the cache
"installed".

Fix: walk to the inner-weights identity. Each subclass exposes a
different field — `ControlNet.control_model`, `ControlLora.control_weights`,
`T2IAdapter.t2i_model`. New helper `_resolve_controlnet_inner` checks
those in order and falls back to the wrapper for unknown subclasses
(equivalent to disabled cache for that path — conservative).

Cache key updated to `(id(unet), id(_resolve_controlnet_inner(controlnet)))`.
`_CacheEntry` now weakrefs the inner-weights object instead of the
wrapper, so the entry's `is_alive()` check stays valid across rebuilt
wrappers.

Regression tests added:
- `test_fresh_wrapper_around_shared_weights_hits_cache` — the actual
  production scenario.
- `test_different_inner_weights_do_not_collide` — guards against the
  inverse failure (wrong cache hit on checkpoint swap).
- Three resolver tests for the per-subclass field paths.

`pytest tests/` → 337 passed, 4 skipped.

## Second follow-up — monkey-patch didn't reach the consumer

Even after the inner-weights fix tested clean, the production log
*still* showed `Requested to load KModel / ControlNet` and
`Moving model(s) has taken X seconds` on every gen. Three parallel
investigation agents converged on the diagnosis: our monkey-patch
on `backend.patcher.controlnet.apply_controlnet_advanced` never
intercepted the actual call site.

Reason: `modules_forge/supported_controlnet.py:9-12` does

```python
from backend.patcher.controlnet import (
    ...
    apply_controlnet_advanced,
    ...
)
```

Python's `from X import Y` copies the reference into the consumer's
namespace **at import time**. The consumer's `apply_controlnet_advanced`
is a local copy of the original function, captured before our
patch ran. Rebinding `backend.patcher.controlnet.apply_controlnet_advanced`
later doesn't update that local copy. The call at
`supported_controlnet.py:207` continued invoking the original, the
cache was dead code, and the install log line was misleadingly
optimistic.

Same Python gotcha as `unittest.mock.patch` documents: you have to
patch the consumer's local binding, not just the source module.

Fix: install path now also walks a `_KNOWN_CONSUMER_MODULES` tuple
(currently just `modules_forge.supported_controlnet`) and rebinds
each consumer's local symbol IF it currently points at the original.
Identity guard prevents clobbering competing extensions that have
their own version. Unloaded consumers are skipped silently — their
later import will pick up our wrapped function from the source.

Boot log now reports the rebind count:

```
cplugapi: patched backend.patcher.controlnet.apply_controlnet_advanced
(controlnet patcher cache, enabled; rebound 1 consumer(s))
```

Three regression tests added: rebind happens for known consumers,
custom bindings are NOT clobbered (warning logged), missing consumer
modules don't crash install.

### Architectural lesson

Two separate `from-import` blast radii missed in 24 hours (this fix
+ the late-abort hook earlier). Worth pinning as a project rule for
any future monkey-patch under `modules/cplugapi/`:

> Before declaring a monkey-patch "installed", grep the codebase for
> `from <target.module> import <symbol>` and rebind every consumer
> that captures the symbol locally. The source module patch alone
> only catches `module.symbol(...)` call patterns, not `symbol(...)`
> after a `from-import`.

The `_KNOWN_CONSUMER_MODULES` constant explicitly lists the
consumers we audited — adding a new monkey-patch should re-grep and
extend the list. Don't iterate `sys.modules` automatically; the
explicit list is auditable and survives upstream rebases.

`pytest tests/` → 340 passed, 4 skipped.

## Third follow-up — cleanup() unloads what we just cached

After the consumer-rebind fix landed, KModel detach/reattach and
"Moving model(s) has taken X seconds" both disappeared from gen 2+.
But "Requested to load ControlNet" still fired every gen.

Root cause: `backend/patcher/controlnet.py:358-362`

```python
def cleanup(self):
    self.model_sampling_current = None
    if getattr(self, "control_model_wrapped", None) is not None:
        memory_management.unload_model(self.control_model_wrapped)
    super().cleanup()
```

`sampling_cleanup` walks `unet.list_controlnets()` and calls
`cnet.cleanup()` at the end of every gen. For our cached cnet,
`unload_model(cnet.control_model_wrapped)` pops it from
`current_loaded_models`. The next gen's `load_models_gpu` lookup
misses → "Requested to load ControlNet" log fires → entry
re-inserted (cheap, no weight movement, but the noise + churn is
visible).

Fix: monkey-patch `ControlNet.cleanup` to skip the `unload_model`
call when the cnet carries our `_cplug_cache_held` tag. Untagged
cnets (non-cached path or competing extensions) clean up normally.

Implementation: rather than reimplement cleanup inline (which would
fork from any future upstream changes), we temporarily swap
`memory_management.unload_model` with a no-op while invoking the
original cleanup, then restore. Try/finally guard around the swap.

Cnet tagging happens at cache-install time:
```python
cached_cnet = result.controlnet_linked_list
if cached_cnet is not None:
    cached_cnet._cplug_cache_held = True
```

Boot log now reports both layers:
```
cplugapi: patched backend.patcher.controlnet.apply_controlnet_advanced
(controlnet patcher cache, enabled; rebound 1 consumer(s);
 cleanup-skip installed)
```

Four new tests:
- `test_cnet_tagged_when_cached` — tag attached on install
- `test_install_patches_controlnet_cleanup` — cleanup wrapper installed,
  idempotent on re-install
- `test_wrapped_cleanup_skips_unload_for_tagged_cnet` — tagged cnets
  skip unload_model; untagged go through normally
- `test_wrapped_cleanup_restores_unload_after_call` — try/finally
  restores the symbol so concurrent cleanups don't see corrupt state

`pytest tests/` → 344 passed, 4 skipped.

### Architectural note

The cache is now a two-layer construct: (1) cache the patched UNet
clone, (2) skip the cleanup unload that would invalidate the cache
between gens. The two layers are necessary AND sufficient — neither
alone gets us a quiet log. Future maintainers should treat them as
a unit; disabling either via `CPLUG_CONTROLNET_CACHE=0` flips both
back to upstream behaviour (the wrapped cleanup short-circuits when
emission_enabled is false because the cnet won't carry the tag).
