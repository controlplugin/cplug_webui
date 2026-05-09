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
