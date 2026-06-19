# 2026-06-18 — Upstream Forge-Neo catch-up merge

**Symptom / motivation.** The fork was 54 commits ahead of merge-base
`addf9dcc` and 104 behind `upstream/neo`, missing a wave of upstream
robustness work (OOM `is_oom` fallback, real `test_for_nans` recovery,
a VRAM-freeing `unload_model_weights`, the `/sdapi` extras arg-shift fix,
the `res_step` option, a docker tree) plus ~90 internal refactors.

**Integration model — merge, not 54-commit rebase.** A literal rebase
surfaces conflicts one commit at a time (sequential, hard to parallelise,
history-rewriting). For a staged, reviewable, rollback-friendly catch-up
we merged `upstream/neo` into a throwaway branch `integrate/upstream-neo`
in an isolated worktree. `main` was never checked out or modified.
Restore point: tag `backup/main-2026-06-18-pre-neo` at the pre-merge
`main` (`a1052bcf`).

**Conflict surface.** Git auto-merged all but 6 files. The 6 resolved
conflicts, plus 8 auto-merged-but-fork-critical files that were
explicitly re-verified (auto-merge can silently drop a non-conflicting
fork edit):

Resolved conflicts:
- `backend/args.py` — preserved fork flags `--no-channels-last`,
  `--expandable-segments`, `--fp32-vae`; adopted upstream's SageAttention
  flag collapse (`--sage2-*` → single `--sage-function`) and
  `--enable-triton-backend`.
- `modules/processing.py` — kept upstream's de-autocast +
  `args.dynamic_args.last_extra_generation_params` flow while re-applying
  the fork's process-wide prompt LRU. **Bug caught:** upstream `.clear()`s
  that params reference before the fork cache `.put()` ran — would have
  cached an empty dict; fixed by snapshotting with `.copy()`. Preserved
  `clear_prompt_cache()` on model swap and the relocated post-decode
  `test_for_nans`.
- `modules_forge/uv_hook.py` — adopted upstream `patch(symlink, local)` /
  `_set_cache` / `--uv-local-cache`; re-grafted the fork's `_ensure_uv`
  pip bootstrap into `_pre_check`; gated the blocking `input()` behind an
  `isatty()`/`CI` check so containers/CI cannot hang.
- `modules_forge/initialization.py` — kept the fork's inline
  `expandable_segments` implementation; dropped upstream's now-unused
  `try_expandable_segments` import.
- `modules_forge/forge_version.py` — kept fork branding (`cplug_webui` / `2.22`).
- `README.md` — kept the fork stub/branding (did not pull upstream's full README).

Headless purity: introduced `modules/cplugapi/resolution.py` exposing a
round-to-nearest `sRound` / `step` byte-identical to upstream
`modules.ui.sRound`, and rewired `processing.py`, `img2img.py`, and the
lazy import in `infotext_utils.py` to it, so the API-imported processing
path no longer pulls Gradio via `modules.ui`. (`ui_loadsave.py` keeps its
lazy `modules.ui` import — genuinely UI-only.)

Auto-merged fork edits re-verified intact: `get_attn_precision`
early-return fix (`backend/attention.py`), psutil RAM cache + bf16
`vae_dtype` + `soft_empty_cache` debounce (`backend/memory_management.py`),
`_autoscale_tile` + single-pass tiled VAE (`backend/patcher/vae.py`),
`_FEATHER_STEPS` (`sampling_function.py`), `_cplug_tome_cache`
(`sd_models.py`), `_beta_ppf` LRU (`sd_schedulers.py`), live-preview
decoder download lock (`sd_vae_taesd.py`), version branding (`ui.py`).

**Blast radius.** All `modules/cplugapi/` monkey-patch targets
(`apply_controlnet_advanced` / `ControlNet.cleanup`, `LoadedModel.is_dead`,
UNet/ToMe clone cache) were confirmed untouched by upstream. The one
`/sdapi`-output deviation to keep watching is the fork's bf16 `vae_dtype`
(gated by `--fp32-vae`), now interacting with upstream's `inference_cast`.

**Verification.** No conflict markers remain; all 104 merge-changed `.py`
files `py_compile` clean; `processing.py` has no `modules.ui` import.
Pending: full `tests/cplugapi` suite + a runtime import/OOM smoke.

**Follow-up (not in this merge).** The genuinely new fork work from the
plan is deferred to its own commits: `controlnet_cache.clear_cache()` +
unload hook, headless OOM auto-recovery wrapping `process_images_inner`,
and editing the rebase-delivered `docker/` tree for the fork.

**Rollback.** `git worktree remove ../cplug_webui-neo-integrate` and
`git branch -D integrate/upstream-neo`; `main` is unchanged at
`backup/main-2026-06-18-pre-neo`.
