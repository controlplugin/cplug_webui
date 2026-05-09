# Changes

Fork-specific changes only. Upstream Forge Neo changes flow in via rebase
and are not duplicated here. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely — chronological,
grouped by **Added / Changed / Fixed / Removed**.

## Unreleased

### Changed — diagnostic logs default off in `webui-user.bat`

The three diagnostic streams (`/cplugapi/v1/*` access log,
`/sdapi/v1/*` request observer, per-gen pipeline timing) are now
toggled off by default in the fork's launcher. Each stream is
controlled by its own env var, all read once at install time:

| Var | Stream |
|---|---|
| `CPLUG_ACCESS_LOG` | `cplugapi.access` (per-cplugapi-request line) |
| `CPLUG_SDAPI_OBSERVER` | `cplugapi.sdapi` (per-`/sdapi/v1/*` line) |
| `CPLUG_GEN_TIMING` | `cplugapi.gen_timing` (per-gen `total_ms`/`vae_decode_ms`/`peak_vram_mb`) |

`webui-user.bat` sets all three to `0` because the desktop client
polls progress at ~4 Hz during a gen, which makes any of these
streams loud enough to drown out warnings/errors during normal
operation. Operators triaging client behavior flip the relevant
toggle to `1` and restart.

Module-level default remains "enabled" so devs running tests or
booting without the launcher get the diagnostic output without
needing extra setup. Capability registration tracks the toggle —
disabled streams are absent from `/health.capabilities[]`, which
lets a client detect "log routing is off" without round-tripping
to a config endpoint.
(`modules/cplugapi/access_log.py`, `sdapi_observer.py`,
`gen_timing.py`, `webui-user.bat`)

### Added — auto-preempt for `/sdapi/v1/{txt2img,img2img}`

Two-stage mechanism. **Pre-handler middleware**: pure-ASGI, fires
`shared.state.interrupt()` and drains pending tasks into
`cancelled_tasks` before forwarding incoming gen requests. **Late-
abort hook on `process_images_inner`**: re-arms `state.interrupted =
True` at entry if the active task is in `cancelled_tasks` —
necessary because Forge's `state.begin()` clears the interrupt flag
inside the queue_lock critical section, so without this hook
queued-but-cancelled gens run to completion despite the registry
marker.

Result: rapid sketch strokes from the desktop client stop stacking
up behind one another. Each preempted gen exits on its first
sample-step interrupt-check (~100 ms wall) instead of running 13+
diffusion steps.

Mode-driven via `CPLUG_PREEMPT_MODE`:
- `always` (fork default) — every gen request preempts. Best for
  live-preview sketch workflows where the most recent stroke is
  the one that matters.
- `header` — only when `X-Cplug-Preempt: 1` is set; per-request
  opt-in for clients that want to mark some gens as terminal.
- `off` — pure passthrough, equivalent to upstream behavior.

Pre-handler ordering: by the time Forge's handler tries to acquire
`queue_lock`, the previously-running gen has been told to stop.
The new gen waits ~1 sample step (~80 ms) for the cancelled gen to
notice `state.interrupted` and exit, then runs normally.

Read-only on the upstream surface — when no preempt fires, pure
passthrough. Capability advertises mode (`sdapi/preempt-always` /
`sdapi/preempt-header`) so clients can detect active behavior
without a config endpoint round-trip.
(`modules/cplugapi/auto_preempt.py`)

### Added — `POST /cplugapi/v1/session/preempt`

Cancel-without-knowing-the-id companion to `/session/cancel/{id_task}`.
Fires `shared.state.interrupt()` if a task is running, records it in
`cancelled_tasks` (so late status pokes return `"already_cancelled"`),
and optionally drains the pending queue with `?clear_pending=1`.
Returns the cancelled task id, a `was_running` flag, and the
pending-drain count for diagnostic clarity.

Designed for the sketch-mode pipelining pattern: client fires preempt
+ next gen back-to-back, the new `/sdapi/v1/txt2img` blocks ~1
sample step on Forge's `queue_lock` while the cancelled gen exits,
then proceeds normally. Capability: `session/preempt`.
(`modules/cplugapi/session_preempt.py`)

### Fixed — Windows asyncio `ConnectionResetError` traceback spam

asyncio's Windows ``ProactorEventLoop`` calls
``ProactorBasePipeTransport._call_connection_lost`` when a TCP
transport closes; the cleanup tries ``socket.shutdown(SHUT_RDWR)``
which raises ``WinError 10054`` (ConnectionResetError) when the peer
sent RST instead of FIN. Python's default asyncio handler logs this
as ``Exception in callback…`` even though the cleanup itself is
benign — the connection is already gone, no work is lost.

The desktop ControlPlugin client closes connections this way
routinely (preempting in-flight gens for fresh sketch strokes), so
without filtering the log fills with these tracebacks. New
`modules/cplugapi/asyncio_filter.py` wraps the running loop's
exception handler to recognise this specific signature and demote
it to DEBUG. Real ConnectionResetErrors elsewhere in the app pass
through unchanged. Windows-only, idempotent.

### Added — `/sdapi/v1/*` request observer + console-routed cplugapi loggers

`cplugapi.sdapi` logger now emits one structured line per
`/sdapi/v1/*` request: method, path, status, dur_ms, request
Content-Length. Implemented as a pure-ASGI middleware (NOT
`BaseHTTPMiddleware`) so it sidesteps the streaming-response wrapper
bug and can sit on Gradio long-poll endpoints without breaking them.
Read-only — preserves byte-identity on the upstream surface.
Capability: `sdapi-request-log`.

The diagnostic intent is "what is the desktop client triggering" —
when the artist sees gens fire on canvas with no apparent action on
their side, this log shows exactly which endpoint was called, when,
and how long it took.

The `cplugapi.access` and `cplugapi.gen_timing` loggers are now
routed through Forge's `backend.logging.setup_logger`, so their
lines appear on console alongside Forge's own boot output (same
`name :: INFO` rendering). Previously these messages were dropped
because Forge's default Python logging config silences INFO-level
messages on stderr.
(`modules/cplugapi/sdapi_observer.py`,
`modules/cplugapi/access_log.py`, `modules/cplugapi/gen_timing.py`)

### Fixed — `RuntimeError: No response returned` on Gradio streaming paths

All four cplugapi middlewares (access_log, request_id, security,
idempotency) inherit from Starlette's `BaseHTTPMiddleware`, which
wraps the entire request lifecycle through anyio task groups that
buffer the response via a memory-channel. When a downstream endpoint
returned a `StreamingResponse` whose generator raised mid-stream
(typical for Gradio's long-poll endpoints on client disconnect), the
channel-based plumbing converted the real exception into a spurious
`RuntimeError: No response returned`, masking the actual cause and
firing through the entire middleware chain even on paths outside
`/cplugapi/v1/*`. Documented at encode/starlette#1438.

Each middleware now overrides `__call__` to bypass the wrapper for
non-cplugapi paths — pure passthrough via `await self.app(scope,
receive, send)`, which leaves the response shape identical to
"middleware not installed". The `dispatch` method is preserved for
in-prefix requests where we genuinely need to inspect / time / cache
the response. (`modules/cplugapi/access_log.py`,
`request_id.py`, `security_middleware.py`, `idempotency.py`)

### Fixed — `apply_token_merging` cloned the UnetPatcher every gen

Forge's design has `TomePatcher.patch` deep-clone the UnetPatcher
and attach attn1 patches, returning a new patcher instance. The
fresh clone never `__eq__`s the patcher already loaded, so
`load_models_gpu` (`backend/memory_management.py:626-650`) takes the
`is_clone` branch every generation: pop the previously-loaded
patcher, detach its patches, attach to the new clone. Side effects:

- `Requested to load KModel` logs every single gen even though no
  weights actually move.
- ~0.7s wasted per gen rebinding patches.

The fork now caches the patched UnetPatcher per `(LoRA-baseline-id,
ratio)`. Stable LoRA + stable ratio (the sketch workflow) → cache
hit → cached patcher reassigned directly to `forge_objects.unet`,
so the `__eq__` check in `load_models_gpu` matches and the
unload/reload path is skipped entirely. LoRA change or ratio change
naturally invalidates via the key. (`modules/sd_models.py`)

### Added — generation pipeline timing log

`cplugapi.gen_timing` logger emits one structured line per
`process_images_inner` call: `total_ms` for end-to-end, `vae_decode_ms`
summed across `decode_latent_batch` calls (HR-pass accumulates),
`peak_vram_mb` for the per-gen VRAM watermark (reset before each
gen), plus an `error=<ExceptionName>` field on raised gens. Joined
to the sampler's existing tqdm time, the residual of `total_ms` is
pre-sampling cost (conditioning, init prep, kernel JIT) — the bucket
that grows when models are evicted/reloaded between gens.

`peak_vram_mb` is the diagnostic for "is the NVIDIA driver silently
spilling VRAM to shared memory over PCIe?" — when peak approaches
total VRAM during sampling, sysmem fallback may engage and slow the
gen by 10-20×. Disable via NVIDIA Control Panel → CUDA - Sysmem
Fallback Policy → "Prefer No Sysmem Fallback".

Read-only wrap on upstream functions; never mutates response bytes
so `/sdapi/v1/*` byte-identity holds. Idempotent install. Capability
string: `gen-timing`. (`modules/cplugapi/gen_timing.py`)

### Changed — launcher defaults: `--highvram`

Adds `--highvram` to `webui-user.bat`'s `COMMANDLINE_ARGS`. Forge
defaults to evicting models from VRAM after use; on a 12 GB+ card
that costs an unnecessary reload between gens (UNet, text encoders,
VAE). `--highvram` keeps loaded models pinned, which removes the
between-gens "Requested to load X / Moving model(s) has taken Ns"
chatter from the log and shaves seconds off the perceived latency
of consecutive gens. `--gpu-only` is the more aggressive variant
(documented inline in the .bat) for 16 GB+ deployments that want
zero offloading. (`webui-user.bat`)

### Fixed — TOCTOU race in TAESD / VAEApprox live-preview downloads

Two concurrent generations both triggering live-preview decoder
download to the same path would each call
`torch.hub.download_url_to_file(url, path)` after both had already
seen `os.path.exists(path) == False`. The two writes interleaved on
the same file descriptor, producing a torn `.pth` that failed the
next `torch.load` with `PytorchStreamReader failed reading file
data/N`. Forge then renamed the file to `.corrupted`, leaving the
second consumer with `FileNotFoundError`.

Both `modules/sd_vae_taesd.py` and `modules/sd_vae_approx.py` now
serialise downloads of the same path through a per-path
`threading.Lock` (different paths still parallelise) and publish
atomically — the network write goes to a `.part` sibling, then
`os.replace` swings the final path into place. A crash mid-download
leaves only an orphan `.part`; the next call still sees the canonical
path absent and re-downloads cleanly. (`modules/sd_vae_taesd.py`,
`modules/sd_vae_approx.py`)

### Added — per-request access log for `/cplugapi/v1/*`

One structured line per request emitted to the `cplugapi.access`
logger at INFO level. Captures end-to-end server-side wall time
(`dur_ms`), method/path/status, request and response Content-Length,
the `X-Request-Id` value (so client and server logs join cleanly),
and a `replayed=1` flag for idempotency-cache hits so cached responses
are distinguishable from real handler executions. Outside
`/cplugapi/v1/*` the middleware is a straight pass-through — preserves
the byte-identity invariant for `/sdapi/v1/*`. Disable per-deployment
via `CPLUG_ACCESS_LOG=0`. Capability string: `request-log`.

Diagnostic intent: when the desktop client reports slow workflows,
this log lets operators triage server-side vs network-vs-client by
comparing the client's measured RTT against the server's `dur_ms`.
(`modules/cplugapi/access_log.py`)

### Added — pickle-format checkpoint classification

`.ckpt` / `.pt` / `.pth` / `.bin` files now go through a second-stage
classifier on `header_peek` fallthrough: `torch.load(weights_only=True,
map_location="meta", mmap=True)` to read the state-dict shape without
materialising tensor data and without running unrestricted unpickling.
The same `classify_state_keys` sentinels apply, so a legacy A1111-era
SDXL `.ckpt` now reports `arch: "sdxl"` identically to its safetensors
sibling. Cost: hundreds of ms cold-scan vs ~ms for safetensors —
amortised by the existing LRU cache. Common wrappers handled:
`{"state_dict": ...}` (A1111), `{"model": ...}` (Lightning), bare
state-dict (clean dumps).

`.gguf` remains unsupported but now surfaces under a dedicated
`gguf_unsupported` error code so the client can distinguish "format
gap" from "tried and failed". (`modules/cplugapi/pickle_peek.py`,
`modules/cplugapi/models_disk.py`)

### Fixed — `.ckpt` / `.gguf` mis-classified as `not_a_checkpoint`

Live testing surfaced that `cardesigner.ckpt` (a real loadable Forge
model) was being tagged `arch: not_a_checkpoint, error: unsupported_format`
in `/cplugapi/v1/models/sd-checkpoints`, which would have caused the
desktop client to hide it from every mode picker. Pickle-format
checkpoints are loadable, just not classifiable from outside without
unpickling — so they now report `arch: unknown` with a new
`pickle_format` error code. `unsupported_format` is now reserved for
genuine non-checkpoints (`.safetensors.index.json` sharded manifests).
Same split applied to transient I/O failures (`model_not_found`,
`permission_denied`) and corrupt-header (`invalid_safetensors`) — all
map to `unknown` since the underlying file may still be a real model.

### Added — model architecture detection (`/cplugapi/v1/models/*`)

Three new endpoints under the cplugapi surface so the desktop
ControlPlugin client can populate model lists per arch and grey out
empty modes without filename heuristics.

- `GET /cplugapi/v1/models/active` — arch of the currently-loaded
  checkpoint, derived from Forge's loaded engine class. Capability
  string `models/architecture`. Always 200; `loaded=false` is a state,
  not an error.
- `GET /cplugapi/v1/models/sd-checkpoints` — full disk listing
  decorated with `arch`, `dtype`, and per-file `error`. Strict superset
  of `/sdapi/v1/sd-models`. Includes top-level `available_arches`
  summary. Capability string `models/disk-scan`.
- `GET /cplugapi/v1/models/architectures` — lightweight summary
  endpoint returning just `available_arches`. Drives the mode-picker UI
  without paying the full listing's wire cost. Capability string
  `models/architectures-available`.

Detection is two-tier: loaded models map through a static
`ENGINE_CLASS_TO_ARCH` table; on-disk checkpoints are classified by
peeking the safetensors JSON header (raw `struct` read, no `safe_open`
mmap) and matching state-dict key sentinels against per-arch tables
(SD15/SDXL/SDXL-refiner/SD2/SD3/Flux/Flux-Schnell/PixArt/Lumina-2/
Hunyuan-DiT/Cascade-B/Cascade-C). SAI Model Spec
`modelspec.architecture` is honoured as a fast-path when present.
Disk-scan results are cached behind a content-addressable
`(path, mtime, size, ino)` LRU keyed cache; cap configurable via
`CPLUG_MODELS_CACHE_MAX` (default 4096).

New modules:
- `modules/cplugapi/arch.py` — vocabulary + classifier (pure functions, no I/O)
- `modules/cplugapi/header_peek.py` — raw safetensors header reader
- `modules/cplugapi/models_disk.py` — LRU cache + classify-on-read
- `modules/cplugapi/active_model.py` — endpoint (1)
- `modules/cplugapi/sd_checkpoints.py` — endpoint (2)
- `modules/cplugapi/architectures.py` — endpoint (2b)

Test coverage: 7 new test files, 75+ new test cases. Total cplugapi
suite: 224 passing, 3 platform-skipped.

### Added — img2img blank-canvas at full denoise

When `denoising_strength == 1.0` and no `init_img` is supplied, the
img2img pipeline now synthesizes a blank RGB canvas at the requested
dimensions instead of crashing in `processing.py`'s
`hashlib.md5(img.tobytes())` call. Useful for ControlNet-only runs
where the init image is mathematically discarded anyway. (`modules/img2img.py`)

If an image *is* expected but missing (denoise < 1.0, or sketch/inpaint
modes), the handler now logs a single friendly WARNING line, fires a
Gradio toast, and returns an empty result instead of dumping a deep
`AttributeError` traceback.

### Changed — branding and identification

- Browser tab title: `Stable Diffusion` → `Stable Diffusion : ControlPlugin`
  (`modules/ui.py`).
- Footer: `version: neo` link → `version: cplug_webui` link to fork repo
  (`modules/ui.py`).
- Startup banner + image-metadata `Version:` field: `neo` →
  `cplug_webui`. Generated images now carry `Version: cplug_webui-2.22`
  in their PNG metadata, making fork-generated outputs identifiable
  (`modules_forge/forge_version.py`).

### Changed — launcher defaults

- `webui-user.bat` `COMMANDLINE_ARGS` adds `--cuda-malloc` (Forge's
  startup hint recommends it on Ampere+ devices). Combined with the
  earlier `--api --xformers --uv` defaults from commit `6d06bf4d`.

### Fixed — `--uv` bootstrap on fresh venvs

`modules_forge/uv_hook.py` now self-bootstraps: if the `uv` binary
isn't on PATH when the launcher's default `--uv` flag runs, it
pip-installs `uv` into the active venv before patching `subprocess.run`.
Falls back to plain pip if the bootstrap itself fails. Resolves the
`'uv' is not recognized` error users hit on first boot after the
`--uv` flag became a default.

### Fixed — cplugapi middleware mounted post-launch

`modules/cplugapi/router.py` now installs `idempotency`, `request_id`,
and `security_middleware` via direct `app.user_middleware.insert` +
`app.middleware_stack = app.build_middleware_stack()`, mirroring
`modules/initialize_util.py:setup_middleware`'s idiom for GZip/CORS.
The previous `app.add_middleware(...)` calls raised
`RuntimeError: Cannot add middleware after an application has started`
because `Api(app)` is constructed after `shared.demo.launch()` has
already served Gradio's internal startup requests (which builds and
caches the middleware stack). Affected every boot with `--api`
enabled — i.e., the launcher's default config since commit `6d06bf4d`.

## Earlier (selected)

For full history, see `git log`. Notable fork milestones:

- `4827ed1c` — `chore(launcher): enable --cuda-malloc by default`
- `361b74ce` — `fix(cplugapi): mount middlewares post-launch via user_middleware`
- `03cbe3b8` — `fix(launcher): bootstrap uv when missing; rename banner to cplug_webui`
- `5bc02172` — `+ visible version identification, blank input image when denoise is 1.0 and no image is specified`
- `f0c62796` — `fix(cplugapi): scope prompt LRU key by function identity`
- `34b2416c` — `perf: audit 01 tier 1-3 — caches, defaults, debounce`
- `3a68f10c` — `feat(cplugapi): audit 02 — observability, idempotency, security hardening`
- `e4144569` — `feat(cplugapi): runtime tweaks, prompt cache, forge/preset endpoint`
- `36aac937` — `feat(cplugapi): scaffold /cplugapi/v1/* fork-only API surface`
