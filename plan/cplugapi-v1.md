# cplugapi-v1 — local execution checklist

The authoritative spec is `D:/GitHub/ControlPlugin_WebUI/plan/05-cplugapi-v1/plan.md`
(post-eval-r5, SOLID). This file tracks **what has actually landed in
cplug_webui** against that spec, plus any local decisions worth recording.

Status: **Phase 1 complete.** 44/44 cplugapi tests passing, ruff clean,
CI workflow + OpenAPI export wired.

---

## Phase 1 — Plumbing + read-only + cancel (~1–2 weeks)

Ship these together as a release. Closes the client misclassification gotcha
and unblocks Track 02's capability-detection / graceful-degrade work without
touching the diffusion pipeline.

- [x] **T17** — scaffolded `modules/cplugapi/` package; `setup_cplugapi(app, auth_dependency=...)` injection at `modules/api/api.py:248-249`; `_ping` smoke endpoint at `/cplugapi/v1/_ping`. Idempotent + thread-safe via `app.state.cplugapi_mounted` flag guarded by module-level lock.
- [x] **T18** — `modules/cplugapi/capabilities.py` — `register(name, predicate)`, `enabled_capabilities()`, `unregister()`, `reset()`. Slash-only enforcement (raises on dot notation). Predicate exceptions logged at WARNING (not silent). Thread-safe.
- [x] **T19** — `__version__.py` reads `CPLUG_FORK_COMMIT`, `CPLUG_UPSTREAM_COMMIT`, `CPLUG_FORK_BUILD_DATE` env vars with safe fallbacks (`"unknown"` for SHAs, import-time UTC for date). CI workflow sets all three.
- [x] **T20** (light) — `tests/cplugapi/test_no_sdapi_regression.py` asserts cplugapi mount adds only `/cplugapi/v1/*` routes and never touches `/sdapi/v1/*`; second job in CI runs this without booting the webui. Full-model smoke (txt2img@256² 1-step, etc.) deferred to a CI runner with an SDXL fixture.
- [x] **T21** — `GET /cplugapi/v1/identify` returns `{fork, fork_version, fork_commit, upstream, upstream_commit}`. Unauthenticated even when `--api-auth` set. (Note: `fork_commit` is an additive field — see Decisions log §1.)
- [x] **T22** — `scripts/export_cplugapi_openapi.py` + CI step uploads `cplugapi-openapi.json` artifact.
- [x] **T23** — `GET /cplugapi/v1/health` (basic + `?detailed=true`). Forward-compat null/empty placeholders for Phase-3/5/6 fields (`warm_pool_slots`, `active_attention_backend`, `comfy_finalization_tax_active`) so the OpenAPI schema stays stable.
- [x] **T24** — `GET /cplugapi/v1/version` with 60 s cache + shallow-copy on get (no caller-mutation poisoning). Forward-compat placeholders for `attention_backend`, `active_quantization`, `loaded_extensions`.
- [x] **T25** — `cancelled_tasks` registry: thread-safe `add`/`has`/`size`/`reset` with 10-min TTL + 1024-entry hard cap.
- [x] **T26** — `POST /cplugapi/v1/session/cancel/{id_task}` — four states (cancelled/already_cancelled/already_completed/not_found), 200 for all logical cases. Pop precedes the running check; running check re-reads `progress.current_task` immediately before `interrupt()` to narrow the wrong-task race. Path validation: `max_length=128`, regex `^[A-Za-z0-9_:.\-()]+$`. Does NOT take `queue_lock` (see comment at top of file).
- [x] **T27** — race-condition stress test: 1000 cancels × 8 workers + concurrent task lifecycler — no flake, registry stays under cap. Runs in ~0.7 s by exercising `_classify_and_act` directly (HTTP layer covered separately).

## Phase 2 — Preprocess + WebSocket stream (~2 weeks)

- [ ] **T28** — `POST /cplugapi/v1/canvas/preprocess` (§5.5) — multi-module batch, per-module `queue_lock`, strip `data:image/*;base64,` prefix, `asyncio.wait_for` per-module timeout
- [ ] **T29** — partial-failure semantics (one bad module ≠ 4xx)
- [ ] **T30** — event-driven progress source (`asyncio.Event` per active task; idle traffic = exactly 0)
- [ ] **T31** — `WS /cplugapi/v1/session/stream/{id_task}` (§5.6); honor `--api-auth` Basic on upgrade; rate-limit 10/IP, 100/process
- [ ] **T32** — WS load test (100 concurrent, no FD leak after 1000 churn cycles)

## Phase 3 — Warm pool (~1.5 weeks)

- [ ] **T33** — `WarmPool` registry (`modules/cplugapi/warm_pool.py`)
- [ ] **T34** — `sd_models` resolver hook — switch to warm member is <100 ms vs 3–15 s today
- [ ] **T35** — `memory_management.py` integration (GC stops freeing pool residents — direct fix for #1017)
- [ ] **T36** — `POST /preload`, `GET`, `POST /evict` endpoints
- [ ] **T37** — #1017 regression test (100 alternations across 2 checkpoints, no crash)
- [ ] **T38** — feature flag `--cplug-warm-pool` (default off until T37 is solid)

## Phase 4 — Region updates (~3 weeks)

- [ ] **T14a** — license-verify gate for Differential Diffusion port (record SPDX in `plan/dd-license.md`; go/no-go for T15)
- [ ] **T15** — vendor or port `sd-webui-differential-diffusion` into `modules/cplugapi/differential_diffusion.py`
- [ ] **T39** — session latent cache (`modules/cplugapi/session_cache.py`, LRU + VRAM budget)
- [ ] **T40** — `POST /cplugapi/v1/canvas/strokes` (§5.8) — requires warm-pool membership
- [ ] **T41** — bbox-crop optimization (>2× faster on small-mask strokes)

## Phase 5 — Investigations (parallel)

- [ ] **T42** — repro Forge-Neo #936 Comfy-backend finalization tax; decide patch / opt-out / wait
- [ ] **T43** — `--pin-shared-memory` × LoRA leak repro (reForge #458) on Forge Neo
- [ ] **T44** — int8 quality A/B (Forge-Neo #697) per default-bundle model

## Phase 6 — Docs & release

- [ ] **T45** — `docs/cplugapi-v1.md` reference
- [ ] **T46** — `docs/cplugapi-v1-client-guide.md` (capability detection / degrade)
- [ ] **T47** — v2 release notes

---

## Local decisions log

> **2026-05-01 — `/identify` adds a `fork_commit` field** beyond the spec's
> four (`fork`, `fork_version`, `upstream`, `upstream_commit`). Symmetrical
> with `/version`'s `fork_build_commit`. Additive; harmless for serde
> deserializers that ignore unknown fields. Flag for client team in T22's
> OpenAPI release notes; consider adding to spec §5.1.

> **2026-05-01 — Forward-compat placeholders for Phase 3/5/6 fields.**
> `/health?detailed=true` and `/version` emit `null`/`[]`/`False` for
> `warm_pool_slots`, `active_attention_backend`, `comfy_finalization_tax_active`,
> `attention_backend`, `active_quantization`, `loaded_extensions`. Keeps the
> OpenAPI schema stable across Phase-1 → Phase-6 boundaries; client can
> treat them as "not implemented yet" without contract churn.

> **2026-05-01 — `setup_cplugapi` is two lines + import in `modules/api/api.py`,
> not strictly one.** Spec §4 constraint 7 asks for one `include_router(...)`
> call; we have an `if shared.cmd_opts.api_auth` branch inside the call. The
> spirit is rebase-friendliness — diff footprint is 3 comments + 2 code
> lines, well within tolerance. Truly one line would require globals access
> from inside `setup_cplugapi`, which is worse.

> **2026-05-01 — Cross-repo schema mismatch flagged.** Track 02 plan.md:176
> defines `IdentifyResponse { flavor, version, capabilities, upstream }`
> while Track 05 §5.1 (and our impl) uses `{ fork, fork_version, upstream,
> upstream_commit }`. Track 02's own risk register (line 523) calls this
> out. Needs cross-repo resolution; our impl matches Track 05 verbatim,
> so the bug is at the planning layer. **TODO: raise issue in
> ControlPlugin_WebUI/plan/.**

---

## Cross-cutting reminders (from `00-foundation/02-key-decisions.md`)

- **D14** — `/sdapi/v1/*` is byte-identical to upstream; never modify, remove, shadow. Release blocker.
- **D14** — capability strings are slash-only (`canvas/strokes`, never `canvas.strokes`). Open enum — unknown strings log + degrade.
- **D14** — `/cplugapi/v1/*` honors the same `--api-auth` Basic auth as `/sdapi/v1/*`. No second auth layer. **Identify is the one exception** (bootstrap chicken-and-egg per §5.1).
- **D14** — all fork code under `modules/cplugapi/`; one `include_router` line in `modules/api/api.py` (see Decisions §3 above for the actual count).
- **D10** — known upstream bugs to design around: #1017 (checkpoint swap crash), #936 (Comfy finalization tax), #697 (int8 quality), `--pin-shared-memory` × LoRA leak.
- **D11** — `forge_preset` is provider-implicit, NOT in `/health.capabilities[]`. Client gates on `provider == ForgeNeo`.

---

## What landed in this session (2026-05-01)

```
modules/cplugapi/
├── __init__.py
├── __version__.py        — env-driven version constants
├── capabilities.py        — registry with slash-only enforcement
├── cancelled_tasks.py     — TTL+max-size eviction set
├── health.py              — /health (+ detailed mode)
├── identify.py            — /identify (unauthenticated)
├── router.py              — single mount point, idempotent + thread-safe
├── session_cancel.py      — /session/cancel/{id_task}
└── version_endpoint.py    — /version (60 s cache, shallow-copy on get)

scripts/
└── export_cplugapi_openapi.py  — CI artifact builder

tests/cplugapi/
├── conftest.py
├── test_capabilities.py
├── test_cancelled_tasks.py
├── test_no_sdapi_regression.py
├── test_router.py
├── test_session_cancel.py
└── test_session_cancel_race.py

.github/workflows/
└── cplugapi-tests.yml     — pytest + OpenAPI export + sdapi regression

modules/api/api.py         — 5-line setup_cplugapi injection at L245-249
pyproject.toml             — pytest config block
plan/cplugapi-v1.md        — this file
```

Three rounds of code-scrutiny applied (bug-hunt, app-harden, spec-review,
then two refactor passes resolving all Critical/High findings). Verdict
from final pass: **SOLID**.

---

## Audit 01 optimization sprint (2026-05-01)

Source: `plan/optimizations-01.md`. Verified each cited issue, then
landed the changes that fit the rebase / API-stability rules. 56/56
cplugapi tests passing post-sprint, ruff clean.

### Landed

**Phase A — Tier 0 + free defaults**
- [x] **§2.1** — `backend/attention.py:60` — added missing `return` to `get_attn_precision`. Pure bug fix.
- [x] **§2.2** — `backend/text_processing/umt5_engine.py:32,78` — renamed `pad_token` → `id_pad`. Pure bug fix.
- [x] **§3.1** — `cudnn.benchmark` default-on via cplugapi runtime hook (new `modules/cplugapi/runtime.py`); `--autotune` flag still works for explicit upstream parity.
- [x] **§3.3** — `backend/memory_management.py` `vae_dtype()` — bf16 on Ampere+/Ada/Hopper unless `--fp32-vae`.

**Phase B — VAE & tiling**
- [x] **§3.4** — `backend/patcher/vae.py` — single-pass `decode_tiled_` / `encode_tiled_` (was 3-pass average; redundant with feathering); added `_autoscale_tile` helper that grows tile size when free VRAM permits.

**Phase C — Steady-state caches**
- [x] **§4.1** — `modules/cplugapi/prompt_cache.py` (new) — process-wide LRU as second-tier behind upstream's single-slot prompt cache. 32 slots, ~1 MB budget.
- [x] **§4.3** — `backend/patcher/base.py` `patch_weight_to_device` — early-out per key when `_cplug_merged_uuid_per_key[key] == self.patches_uuid`.
- [x] **§4.4** — `backend/patcher/base.py` `add_patches` — cache `set(state_dict().keys())` on the model.
- [x] **§4.5** — `modules/sd_schedulers.py` — `lru_cache` wrapper around `stats.beta.ppf` for the Beta scheduler hot path.
- [x] **§4.6** — `backend/sampling/sampling_function.py` — `_FEATHER_MULT_CACHE` (4-slot LRU) for the no-mask `get_area_and_mult` feathering tensor.
- [x] **§4.13** — `modules/progress.py` — single-entry `_PREVIEW_CACHE` keyed by `id_live_preview`; protected by `_PREVIEW_CACHE_LOCK` (FastAPI threadpool concurrency).

**Phase D — Memory & loader**
- [x] **§5.1** — `backend/memory_management.py` — `_psutil_total_cached` / `_psutil_available_cached` with 500 ms TTL.
- [x] **§5.2** — `backend/memory_management.py` `soft_empty_cache` — 100 ms debounce window; `force=True` bypasses.

**Phase E — Memory format & allocator**
- [x] **§3.2** — `modules/cplugapi/runtime.py` `apply_channels_last` — applied from `sd15.py`, `sdxl.py` (both classes); skipped on Anima/DiT engines. Gated by `--no-channels-last` flag and Ampere+ check.
- [x] **§3.10** — `--expandable-segments` flag (`backend/args.py`) wired into `modules_forge/initialization.py` BEFORE torch CUDA init.

**Phase F — Approximate accelerators**
- [x] **§3.5 / §3.6 / §6.3 / §6.4** — `modules/cplugapi/preset.py` (new) `POST /cplugapi/v1/forge/preset/{name}`. Two presets:
  - `sketch` — TAESD preview, every-5-steps preview, ToMe 0.3, CUDA warmup.
  - `default` — RGB preview, every-step, ToMe 0.0.
- [x] **`forge/preset` capability** advertised in `/health.capabilities[]`.

### Tests added

```
tests/cplugapi/
├── test_prompt_cache.py    — LRU semantics, eviction order, MRU promotion
├── test_runtime.py         — idempotency, no-torch fallback
└── test_preset.py          — sketch/default behavior, capability advertised, 404 for unknown
```

`test_no_sdapi_regression.py` updated for the new endpoint count (5 → 6).

### Deferred (with rationale)

- **§2.3 xformers mask padding** — needs runtime verification against current xformers; the slice-back is not a no-op (it broadcasts the mask q-dim) so the safe minimal change is non-trivial.
- **§3.5 / §3.6 default flips** — would change `/sdapi/v1/*` behavior. Routed through `forge/preset` instead.
- **§3.7 RNG direct-on-device** — misdiagnosed: `randn_source` defaults to `"CPU"` for cross-system reproducibility (modules/shared_options.py:224); the existing GPU branch already exists. Switching the default is a visible image change, not a perf-only change.
- **§4.2 / §4.7 / §4.8 / §4.9 / §4.10 / §4.11 / §4.12 / §4.14** — moderate complexity (hashing image bytes, restructuring batch loop, session-pin) or memory tradeoffs (GGUF dequant cache costs ~4× VRAM); revisit when the live-sketching workload has per-stroke timing data to direct the priority.
- **§5.3 / §5.4 / §5.5 / §5.6 / §5.7 / §5.8 / §5.9 / §5.10 / §5.11 / §5.12 / §5.14 / §5.15** — small individual wins behind larger refactors of `backend/operations.py`, `backend/state_dict.py`, or the offload-stream path. Plan re-evaluation after baseline benchmarks.
- **§6.1 DeepCache patcher / §6.2 TeaCache patcher** — ~30 LOC each per the audit, but require per-architecture validation (SDXL block topology vs Flux double/single blocks). Defer to a dedicated patcher track.
- **§6.5 torch.compile via preset** — extension exists; needs warm-cache wiring and a "warming up" client UX. Defer.
- **§6.6 async VAE decode / §6.7 sub-step cancel** — Track 05 Phase 2 architecture territory.
