# 2026-06-18 — headless OOM auto-recovery (process_images_inner wrap)

**Symptom.** On an out-of-memory error during generation, VRAM stayed occupied
and could wedge the next request on the API path.

**Root cause.** Upstream's OOM auto-recovery (model unload) only fires on the
Gradio UI path — `modules/ui.py` `cleanup()` wired via Gradio `.then(...)`
events. The ControlPlugin API path never reaches it: `/sdapi` + `/cplugapi`
generation calls `process_images(p)` directly under `queue_lock` in
`modules/api/api.py`, never `main_thread.run_and_wait_result`, so
`main_thread.last_exception` is never set and the UI cleanup never runs.

**Decision.** Add headless recovery in the fork layer by wrapping
`modules.processing.process_images_inner` (NOT `api.py` — that would break the
byte-identical invariant). New `memmgmt_patches.install_oom_recovery_hook()`
(separate from `apply()`): wrapper runs the original; on exception it lazily
`from backend import memory_management` and, if `memory_management.is_oom(e)`,
lazily calls `sd_models.unload_model_weights()` (which now also drains the cnet
cache via the unload hook), then RE-RAISES so the request still surfaces a
clean error and the next request starts on freed VRAM. Non-OOM exceptions pass
through untouched; if `backend` can't import (stub env) it re-raises without
classifying — never swallows.

**Ordering.** Installed from `router.py` immediately after
`auto_preempt.install_hooks()` (the last of the three `process_images_inner`
installers: gen_timing → auto_preempt → oom-recovery), making OOM recovery the
OUTERMOST wrapper, so it sees the fully-unwound gen stack (incl.
`ControlNet.cleanup`) before unloading. Idempotent (flag on
`modules.processing`); fail-soft if processing/`process_images_inner` absent.

**Alternatives considered.** Keying off `main_thread.last_exception` (rejected:
inert on the API path); installing inside `apply()` (would be INNERMOST — wrong
order). The single `router.py` line keeps the install order explicit and
auditable.

**Blast radius.** Only `modules/cplugapi/`. One import name + one call line in
`router.py`; no `/sdapi` surface touched. Depends on the controlnet_cache
unload hook (so recovery also frees the cnet cache).

**Failure modes.** If recovery ran during the `_noop_unload` swap it would free
nothing — avoided: `queue_lock` serializes gens and the except fires after the
stack unwinds. Unload failure is caught so it can't mask the original OOM.

**Verification.** `tests/cplugapi/test_memmgmt_patches.py` +6 (wrap, OOM→unload
+re-raise, non-OOM passthrough/no-unload, unload-failure doesn't mask, idempotent,
fail-soft). Full `tests/cplugapi` suite green (565 passed, exit 0). A real
forced-OOM-through-an-API-request smoke still needs a GPU box.

**Rollback.** Revert this commit; the wrap + the one `router.py` line are
self-contained.
