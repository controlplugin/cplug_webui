# 2026-05-01 — Audit 02: Missed-Opportunities Multi-Agent Web Survey

**Audit kind**: external-research scan, not a code review.
**Method**: 8 parallel general-purpose agents on disjoint axes — kernel/runtime, memory/loader, sampler/CFG, live-canvas, API/transport, upstream/ecosystem, security/ops, Photoshop UX. Each agent ran web search + targeted repo reads, returned a ranked report.
**Ground truth**: each agent was briefed on what the sibling planning repo already covers (D-decisions, capability registry, Track 05 phases) so its findings are *additive* to the post-r5 plan.
**Output**: this devlog (findings index), `plan/audit-02-missed-opportunities.md` (phased actionable plan), and the code that landed today.

---

## 1. Findings index

Eight reports, ranked by leverage / cost ratio. The full content is preserved in conversation transcripts; this section condenses each axis to its top-3 actionable picks.

### Axis A — Inference kernel / runtime (4070-class GPUs)

1. **Nunchaku SVDQuant for SDXL** (Jan 2026 v1.2 ships SDXL UNetLoader + LoRA + ControlNet). UNet 5 GB FP16 → 1.5 GB INT4. A 24 GB 4090 holds 8-12 INT4 SDXL UNets simultaneously — re-shapes the warm-pool math entirely. Apache-2.0; per-checkpoint baking ~1-2 min. Capability `runtime/svdquant`.
2. **First-Block-Cache (FBCache) + TeaCache** — `DenOfEquity/sd-forge-blockcache` is a working Forge port. ~2-3× per step on SDXL with threshold ~0.3, near-zero quality loss. MIT.
3. **`torch.compile` MegaCache** — `save_cache_artifacts` / `load_cache_artifacts` (PyTorch 2.7+). Cold-start UNet compile drops 90-180 s → <2 s. Survives restart. **LANDED** today as `modules/cplugapi/megacache.py`.

Minor: pin SageAttention `2++` (not `3` which is Blackwell-only); `torch.compile` `max-autotune` + CUDAGraph Trees over the current `reduce-overhead` mode; AOT-Inductor for shipped-binary artifacts.

Source links: [Nunchaku](https://github.com/nunchaku-ai/nunchaku), [SVDQuant blog](https://hanlab.mit.edu/blog/svdquant), [FBCache (WaveSpeed)](https://github.com/chengzeyi/Comfy-WaveSpeed), [Forge block-cache port](https://github.com/DenOfEquity/sd-forge-blockcache), [TeaCache](https://github.com/ali-vilab/TeaCache), [SageAttention 2++ paper](https://arxiv.org/abs/2505.21136), [PyTorch caching tutorial](https://docs.pytorch.org/tutorials/recipes/torch_compile_caching_tutorial.html), [AOTInductor docs](https://docs.pytorch.org/docs/stable/user_guide/torch_compiler/torch.compiler_aot_inductor.html), [DC-AE paper](https://arxiv.org/abs/2410.10733), [AGD adapter](https://arxiv.org/abs/2503.07274), [AsyncDiff](https://github.com/czg1225/AsyncDiff).

### Axis B — Live-canvas / latent extrapolation

1. **Self-Forcing / Causal Forcing distillation paradigm** (Krea Realtime 14B is the public proof). Trajectory distillation under exposure to the model's own past errors — exactly the regime live-painting creates. No SDXL artifact yet (only video). Track for SDXL ports; capability `forge/distillation-flavor`.
2. **PELC / DecFormer for wet-dry seam** (NeurIPS / Dec 2025) — 7.7M-param transformer fixing linear-blend-in-latent-space artifact at mask boundaries. Up to 53% edge-error reduction. Pairs cleanly with Differential Diffusion. Capability `canvas/composite.mode`.
3. **ControlNeXt for stroke conditioning** — replaces ControlNet's full encoder branch with a small mid-block conv via Cross Normalization. ~10% the params; live mode runs the encoder every frame so this directly halves stroke-conditioning latency. Capability `canvas/strokes.encoder`.

Honourable mentions: SANA-Sprint / FLUX.2-klein realtime backbones (no Forge loader yet); StreamDiffusionV2 SLO-aware micro-batcher pattern; OmniGen2 unified conditioning; LayerDiffuse for native-alpha generation; Stroke2Sketch attribute-aware conditioning.

Source links: [Self-Forcing](https://self-forcing.github.io/static/self_forcing.pdf), [Krea Realtime 14B](https://github.com/krea-ai/realtime-video), [SANA-Sprint](https://nvlabs.github.io/Sana/Sprint/), [ControlNeXt](https://arxiv.org/abs/2408.06070), [PELC / DecFormer](https://arxiv.org/abs/2512.05198), [PixPerfect](https://arxiv.org/abs/2512.03247), [OmniGen2](https://comfyui-wiki.com/en/news/2025-06-24-omnigen2-unified-image-generation), [Krita-AI-Diffusion](https://github.com/Acly/krita-ai-diffusion), [sd-forge-layerdiffuse](https://github.com/lllyasviel/sd-forge-layerdiffuse), [StreamDiffusionV2](https://arxiv.org/abs/2511.07399), [Stroke2Sketch](https://arxiv.org/html/2510.16319), [InstantStyle-Plus](https://instantstyle-plus.github.io/), [StyleStudio CVPR2025](https://github.com/Westlake-AGI-Lab/StyleStudio).

### Axis C — Forge-Neo upstream + ecosystem (verified against `Haoming02/sd-webui-forge-classic@neo` ~2026-05-01)

1. **Open issues #694 / #1017** — `LoadedModel.is_dead` crashes when `real_model` is the bare `None` sentinel (set in `__init__` and `model_unload`); Photoshop sessions hit this 100× more than the median user. **LANDED** today as `modules/cplugapi/memmgmt_patches.py` with class-level `_cplugapi_isdead_patched` flag; defense-in-depth `except TypeError` for future drift.
2. **Open issue #1049** — Flux-klein9b OOM at 16 GB; mitigation is `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. **LANDED** today as `modules/cplugapi/cuda_alloc.py` (Linux + CUDA-build only; no-op when operator already set the env).
3. **Differential Diffusion still not in fork** — confirmed `grep differential.diffusion` returns zero hits. Reference implementation: `Panchovix/reForge-DifferentialDiffusion`. ~3-4 days. Precondition for the planned `canvas/strokes` killer feature.

Bonus: `setuptools==69.5.1` is below the CVE-2024-6345 fix; **bumped** today to `70.3.0` in `requirements.txt`. ADetailer is not bundled — de-facto Forge-Neo fork is `Anzhc/aadetailer-reforge`; capability-gate `postprocess/adetailer` rather than vendoring.

Source links: [#694](https://github.com/Haoming02/sd-webui-forge-classic/issues/694), [#1017](https://github.com/Haoming02/sd-webui-forge-classic/issues/1017), [#1049](https://github.com/Haoming02/sd-webui-forge-classic/issues/1049), [#1072 ADetailer](https://github.com/Haoming02/sd-webui-forge-classic/issues/1072), [Anzhc/aadetailer-reforge](https://github.com/Anzhc/aadetailer-reforge), [reForge-DifferentialDiffusion](https://github.com/Panchovix/reForge-DifferentialDiffusion), [setuptools advisory](https://nvd.nist.gov/vuln/detail/CVE-2024-6345).

### Axis D — API / transport / protocol gaps

1. **Idempotency-Key header** (Stripe-style). Without it, a flaky link mid-`canvas/strokes` POST has no safe retry — client either double-renders or gives up. **LANDED** today as `modules/cplugapi/idempotency.py` (LRU 1024 / 24h TTL; caches 2xx + 4xx; `Idempotency-Replayed: true` on hit).
2. **K8s-style `livez` / `readyz` split** — vLLM hit this hard enough to add it; same trap exists for any client that wants to know "model ready vs process up." **LANDED** today as `modules/cplugapi/livez_readyz.py` with public `record_last_error` / `clear_last_error` hooks.
3. **Origin / Sec-Fetch-Site / Host allow-list middleware** — closes CSRF + DNS-rebinding for the localhost-bound desktop deployment. **LANDED** today as `modules/cplugapi/security_middleware.py` (path-scoped to `/cplugapi/v1/*` so `/sdapi/v1/*` byte-identity is preserved).

Deferred: queue introspection endpoint (**LANDED** as `queue_endpoint.py` with rolling EMA), bearer-token auth alongside Basic, WebP frame format on `session/stream` (depends on Phase-5 endpoint not yet implemented), SSE fallback, multipart streaming uploads.

Source links: [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests), [vLLM readiness probes](https://llm-d.ai/docs/usage/readiness-probes), [OWASP WebSocket security](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html), [Localhost dangers — GitHub blog](https://github.blog/security/application-security/localhost-dangers-cors-and-dns-rebinding/), [0.0.0.0 day — Oligo](https://www.oligo.security/blog/0-0-0-0-day-exploiting-localhost-apis-from-the-browser), [Replicate streaming](https://replicate.com/docs/topics/predictions/streaming), [ComfyUI server routes](https://docs.comfy.org/development/comfyui-server/comms_routes), [Acly/comfyui-tooling-nodes](https://github.com/Acly/comfyui-tooling-nodes).

### Axis E — Memory / loader / quantization

1. **Diffusers group offloading + CUDA-stream prefetch** (`enable_group_offload(offload_type="leaf_level", use_stream=True, record_stream=True)`). Turns "doesn't fit" into "fits at 5-10% slowdown." Holds base UNet hot + 2-3 cold UNets in pinned host RAM, streams them per-step. Costs 2× model in CPU RAM.
2. **PEFT `hotswap_adapter` for LoRA** — preserves compile cache across LoRA changes (otherwise the current fuse/unfuse invalidates the graph and defeats MegaCache). 2.04× SDXL on RTX 4090. Apache-2.0.
3. **`fastsafetensors`** — drop-in replacement for `safetensors.load_file`, parallel pread + GDS. ~3 s shaved per cold checkpoint load (Windows NTFS mmap is the worst case). Apache-2.0.

Honourable mentions: speculative model prefetch endpoint; UNet-only swap with shared text-encoder + VAE pool (~2 GB amortized across pool slots); DeepCache for commit path; FP8 SDXL UNet for Ada/Hopper.

Skip: ComfyUI-GGUF for SDXL (city96 explicitly says no — GGUF degrades Conv2d-heavy models; SDXL is conv-heavy; use Nunchaku INT4 instead).

Source links: [diffusers group offload PR #10503](https://github.com/huggingface/diffusers/pull/10503), [PEFT hotswap docs](https://huggingface.co/docs/peft/package_reference/hotswap), [fastsafetensors](https://arxiv.org/html/2505.23072v1), [vLLM fastsafetensor](https://docs.vllm.ai/en/stable/models/extensions/fastsafetensor/), [Outerport SD3.5 cold-start](https://www.outerport.com/blog/sd35-cold-start), [DeepCache](https://github.com/horseee/DeepCache), [NVIDIA TensorRT FP8 SDXL](https://developer.nvidia.com/blog/tensorrt-accelerates-stable-diffusion-nearly-2x-faster-with-8-bit-post-training-quantization/), [city96/ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF), [siliconflow/onediff](https://github.com/siliconflow/onediff), [diffusion fast / PyTorch blog](https://pytorch.org/blog/accelerating-generative-ai-3/).

### Axis F — Sampler / scheduler / CFG

1. **CFG++** (ICLR 2025) — 5-LOC sampler patch (replace renoise step's noise term with unconditional). Documented to fix smoothness on Lightning/Turbo/4-step distilled. Reduces inter-frame flicker — the live-mode failure mode. **Deferred** as Phase D — needs sampler-step touchpoint outside `modules/cplugapi/`.
2. **NAG / VSF** — restore negative-prompt steering on distilled few-step samplers. Lightning/TCD/Hyper officially recommend CFG=1 because positive/negative scores diverge wildly under distillation; VSF (~50 LOC, value-sign-flip) or NAG (~150 LOC, normalized attention guidance) restore meaningful negatives at ~5-10% overhead vs 2× CFG.
3. **PLADIS** (ICCV 2025) — sparse cross-attention; free quality bump on Lightning, ~30 LOC. Stacks with NAG/VSF.

Skip: PAG/SEG (extra UNet forward — wrong direction for live mode), refiner (deprecated), AYS schedule on raw Lightning (artifacts).

Source links: [CFG++ ICLR 2025](https://arxiv.org/abs/2406.08070), [NAG NeurIPS 2025](https://arxiv.org/abs/2505.21179), [VSF](https://arxiv.org/abs/2508.10931), [PLADIS](https://arxiv.org/abs/2503.07677), [AGD](https://arxiv.org/abs/2503.07274), [AYS](https://research.nvidia.com/labs/toronto-ai/AlignYourSteps/), [StreamDiffusion](https://arxiv.org/html/2312.12491v2), [DeepCache](https://github.com/horseee/DeepCache), [Faster-Diffusion](https://github.com/hutaiHang/Faster-Diffusion), [TaylorSeer ICCV 2025](https://arxiv.org/abs/2503.06923), [FDG](https://arxiv.org/abs/2506.19713), [diffusers SDXLCFGCutoffCallback](https://huggingface.co/docs/diffusers/using-diffusers/callback).

### Axis G — Security / ops / observability

P0 issues with concrete file:line references audited live against the repo:

1. **CSRF / cross-origin abuse** — `modules/initialize_util.py:195-210` only attaches `CORSMiddleware` when `--cors-allow-origins` is passed. With Basic creds cached in a browser session, any open tab can drive `POST /sdapi/v1/options` (which mutates global config — `outdir_samples`, `disable_all_extensions`). **Mitigated** today: `security_middleware.py` rejects Origin not in allow-list, Sec-Fetch-Site `cross-site`, and Host `127.0.0.1.evil.example`-style rebinds. Applied only to `/cplugapi/v1/*` (invariant 1 preserves `/sdapi/v1/*` untouched).
2. **DNS rebinding + Host allow-list missing** — same root cause; even bound to 127.0.0.1, a rebinding domain hits the bound port. **Mitigated** today as part of `security_middleware.py`. The `--api-auth` requirement remains the user's call (opt-in).
3. **`setuptools==69.5.1` ships CVE-2024-6345** — direct hit. **Bumped** today to `70.3.0`.

Deferred: P0-3 `/sdapi/v1/options` privilege escalation — invariant 1 means we cannot rewrite the route; the right approach is documented in `plan/audit-02-missed-opportunities.md` Tier 4 — gate POST behind `Sec-Fetch-Site` check (which we now do via the middleware on the cplugapi side, but the sdapi side is still wide open by design). P1-4 .ckpt pickle-load path — needs a `--cplug-strict-models` flag plumbed through `modules/sd_models.py`. P1-5 `Image.MAX_IMAGE_PIXELS` global mutation in `scripts/xyz_grid.py:693`. P1-6 `show_locals=True` exception logging in `modules/api/api.py:178`. Filed; not in this sprint.

Source links: [A1111 #17059 — `py:` prompt RCE](https://github.com/AUTOMATIC1111/stable-diffusion-webui/issues/17059), [CVE-2024-4940 Gradio](https://github.com/AUTOMATIC1111/stable-diffusion-webui/issues/16715), [CVE-2025-64496 Open WebUI RCE](https://nvd.nist.gov/vuln/detail/CVE-2025-64496), [OWASP CSRF cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

### Axis H — Photoshop / UXP / live-AI workflow

1. **`imaging.getPixels` zero-disk binary WS** — UXP 8+ exposes RGBA8/RGBA16 layer pixels via `arrayBuffer`. PS 2025-26 has an officially-supported WebSocket. Today's Forge implied path (HTTP + base64-PNG) burns 15-20 ms encode/decode at 1024² + 33% base64 bloat — fatal at a 50 ms total budget. Capability `canvas/binary-frames`.
2. **Pressure → denoise mapping + tilt → variation** — Wacom Pro Pen 3 reports 8192 pressure levels and 60° tilt; Krita-AI ignores both. Per-vertex `[x,y,p,tx,ty,v,t]` rasterized server-side into a float32 denoise mask. Free differentiator. Capability `canvas/pressure-modulated-denoise`.
3. **Branchable generation history (DAG, not list)** — Magnific Spaces and Invoke have linear history; nobody has the tree. `cplugapi/v1/history.{list,checkout,branch}` with per-node latent persisted under session. Capability `session/history-tree`.

Honourable mentions: Photoshop UXP 50 ms p99 SLO baked into capabilities (`realtime/p99-ms:80`); FLUX.1 Kontext / OmniGen2 character lock; Palette adapter (foreground/background swatch wiring); Whisper-on-device voice prompting (March 2026 Krea shipped this); Smart-Object ↔ generation_id linkage in XMP.

Source links: [FLUX.1 Kontext](https://arxiv.org/html/2506.15742v2), [OmniGen2](https://huggingface.co/blog/azhan77168/omnigen2), [UXP Imaging API](https://developer.adobe.com/photoshop/uxp/2022/ps_reference/media/imaging/), [UXP WebSocket](https://developer.adobe.com/photoshop/uxp/2022/uxp-api/reference-js/Global%20Members/Data%20Transfers/WebSocket/), [Magnific Spaces nodes](https://www.magnific.com/ai/docs/nodes-and-connections), [Invoke release notes](https://support.invoke.ai/support/solutions/articles/151000178246-what-s-new-invoke-release-notes), [Palette-Adapter](https://arxiv.org/html/2509.02000), [Krea Realtime docs](https://docs.krea.ai/user-guide/features/realtime), [BrushNet](https://tencentarc.github.io/BrushNet/), [ProOut ICCV 2025](https://github.com/EadCat/ProOut), [Photoshop Generative Expand](https://helpx.adobe.com/photoshop/desktop/create-open-import-images/create-images/explore-beyond-the-canvas-with-generative-expand.html).

---

## 2. What landed today

Three implementation phases, each delivered by a parallel general-purpose agent on disjoint files; wire-up + verification done in the main session.

### Phase A — Security middleware (`modules/cplugapi/security_middleware.py`)

Path-scoped middleware on `/cplugapi/v1/*` enforcing:

- Origin allow-list (loopback regex + `CPLUG_ALLOWED_ORIGINS` append).
- Sec-Fetch-Site filter (`none` / `same-origin` allowed; `cross-site` / `same-site` rejected).
- Host exact-match allow-list (DNS-rebinding defence; `CPLUG_ALLOWED_HOSTS` append).
- Body-size cap by `Content-Length` (default 32 MiB; `CPLUG_MAX_BODY_BYTES` override).

Three new capabilities: `security/origin-checks`, `security/host-checks`, `security/body-size-cap`. 26 tests in `tests/cplugapi/test_security_middleware.py`.

### Phase B — Observability + idempotency

Four new modules + four test files:

- `modules/cplugapi/request_id.py` — middleware that reads/generates `X-Request-Id` (12-byte URL-safe token, `req_` prefix), echoes on response, stashes on `request.state`. Helper `get_request_id(request)`.
- `modules/cplugapi/idempotency.py` — Stripe-style cache for POST/PUT/PATCH/DELETE. LRU 1024 / 24h TTL (env-tunable via `CPLUG_IDEMPOTENCY_MAX` / `CPLUG_IDEMPOTENCY_TTL_S`). Key validation 8-128 chars `[A-Za-z0-9_:.-]`. Caches 2xx + 4xx. `Idempotency-Replayed: true` on hit. Capability `idempotency`.
- `modules/cplugapi/livez_readyz.py` — `GET /livez` (200 always); `GET /readyz` (200 when torch importable + model loaded + no last-error, else 503). Public `record_last_error` / `clear_last_error` / `get_last_error`. Capabilities `livez`, `readyz`.
- `modules/cplugapi/queue_endpoint.py` — `GET /queue` returning running / pending / history_recent. Rolling EMA estimator (`_EMA_ALPHA=0.2`, window 32). Public `record_completion_ms()`. Capability `queue`.

40 new tests across the 4 test files.

### Phase C — Cold-start + memory-management

Three new modules + four test files:

- `modules/cplugapi/megacache.py` — `configure_env`, `load_artifacts`, `save_artifacts`, `install_atexit`, `apply`. PyTorch 2.7+ MegaCache bundle persisted at `<repo>/cache/inductor/megacache.bin`. Capability `runtime/megacache` predicate-gated on successful load.
- `modules/cplugapi/cuda_alloc.py` — sets `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` early on Linux + CUDA build. Uses `torch.version.cuda` probe (does not initialize CUDA context — would defeat the point). Capability `runtime/expandable-segments`.
- `modules/cplugapi/memmgmt_patches.py` — monkey-patches `backend.memory_management.LoadedModel.is_dead` to short-circuit when `real_model` is the bare `None` sentinel; defense-in-depth `except TypeError`. Capability `memmgmt/issue-694-guard`.

28 new tests.

### Wire-up changes

- `modules/cplugapi/router.py` — imports + `_install_middlewares` ordering (idempotency / request_id / security; outer-first runtime order rejects bad requests before the inner stages do work). Attaches livez/readyz/queue routers to private (auth-gated). All seven new capabilities registered.
- `modules/cplugapi/runtime.py` — `apply_runtime_tweaks()` now calls `cuda_alloc.configure_expandable_segments()` BEFORE anything that could create a CUDA context, then `megacache.apply()`, then `memmgmt_patches.apply()`.
- `tests/conftest.py` — sets `CPLUG_ALLOWED_HOSTS=testserver` at collection time so existing fixtures using `TestClient(app)` (no `base_url` override) keep passing.
- `tests/cplugapi/test_no_sdapi_regression.py` — expected route count 6 → 9 (livez, readyz, queue added).
- `requirements.txt` — `setuptools==69.5.1` → `70.3.0` (CVE-2024-6345).
- `plan/audit-02-missed-opportunities.md` — phased plan (gitignored; for local reference).

### Verification

- `pytest tests/cplugapi/ -q` → 150 passed, 2 skipped (torch-version-conditional).
- `ruff check modules/cplugapi/ tests/cplugapi/` → clean.
- `python scripts/export_cplugapi_openapi.py` → 9 paths exported (was 6).
- End-to-end smoke against TestClient: 12 capabilities advertised on `/health`, all four security rejections fire (Origin / Sec-Fetch-Site / Host / Content-Length), `X-Request-Id` echoed automatically, livez/readyz/queue all 200.

---

## 3. What was deferred (and why)

### Tier 1 follow-ups (sampler quality)

CFG++, VSF, NAG, PLADIS — each requires either editing Forge's k-diffusion sampler step (touches `modules/sd_samplers_*` outside `modules/cplugapi/`, increasing rebase pain) or attention monkey-patching that may interfere with `torch.compile` graph capture and SageAttention dispatch. Right approach: ship as a follow-up extension or a `forge_preset` advanced toggle. Pattern documented in `plan/audit-02-missed-opportunities.md` Phase D.

### Tier 1 follow-ups (FBCache extension copy)

`DenOfEquity/sd-forge-blockcache` is an external extension under `extensions/` (gitignored). The right action is a documented installer note in `cplugapi-v1.md` and possibly an entry in the model-bundle ship list (Track 06). Vendoring would multiply rebase tax.

### Tier 3 architectural items

Nunchaku SVDQuant SDXL, ControlNeXt, PELC, Differential Diffusion port, ADetailer integration, UNet-only-swap pool layout, group offloading, PEFT hotswap, fastsafetensors. Each requires Phase 4-7 of Track 05 to land first; documented for the schedule.

### Tier 4 differentiator UX

Binary WebSocket frame protocol, branchable history DAG, pressure-mod denoise, palette adapter, voice prompting, Smart-Object/generation_id linkage. Cross-track work (02 client + 04 UXP + 05 v2-full); documented.

### Tier 5 watch list

Self-Forcing / Causal Forcing, SANA-Sprint / FLUX.2-klein, AOT-Inductor `.pt2` artifacts, TaylorSeer.

### Out-of-scope by design

Bearer tokens (Basic is sufficient for v1), Prometheus `/metrics` (opt-in flag only), WebTransport/HTTP/3 (no production server stack in 2026), refiner reintroduction.

---

## 4. Open follow-up questions

- Should `cplug_webui` ship a default `--api-auth <random>` rather than leaving auth opt-in? Today the security middleware blocks browser-CSRF, but a local malicious app could still hit 127.0.0.1:7860 without auth. Out-of-scope for this audit; flagged for Track 01 owner.
- Is the planned `canvas/strokes` implementation pressure-aware from day one? If so, the per-vertex schema needs to lock now so the Rust client knows what to send.
- Where does the per-session generation cache (Tier 4 history DAG) actually live on disk? Needs a Track 05 §5.7 / Track 06 (model bundling) coordination decision.

These are not blockers for the v1 ship; they are seed questions for the next plan-eval round.
