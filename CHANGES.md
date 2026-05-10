# Changes

Fork-specific changes only. Upstream Forge Neo changes flow in via rebase
and are not duplicated here. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely — chronological,
grouped by **Added / Changed / Fixed / Removed**.

## Unreleased

### World-class hardening (Phase WA-WC, W1-W18)

A batch of 17 hardening items from `plan/cplugapi-world-class.md`
land together as one release. Three audiences benefit:

- **Desktop loopback** (primary deployment): drop-in upgrade.
  Every existing client keeps working; new behaviour kicks in only
  for clients that opt into it (capability detection on
  `/identify.capabilities[]` is the discovery surface).
- **Cloud single-replica** (newly supported): set
  `CPLUG_DEPLOYMENT_PROFILE=cloud` and the fork flips to a coherent
  cloud-defaults posture (wildcard Host/Origin, rate limit on,
  auto-preempt off, reject-during-drain on). See the new
  `doc/cplugapi-cloud-deploy.md` runbook.
- **Security reviewers**: see the new `doc/cplugapi-threat-model.md`
  for the full posture.

#### Added — RFC 9457 Problem Details error envelope (W3)

Every `/cplugapi/v1/*` error response now uses
`application/problem+json` (RFC 9457; obsoletes 7807) with stable
machine-switchable `code` field, `request_id` for log correlation,
and an `errors[]` array on validation failures. Default FastAPI
behaviour is preserved on `/sdapi/v1/*` — invariant 1
byte-identity intact. Capability: `error-format-problem-details`.
Codes are listed in the error catalog section of `doc/cplugapi.md`.
(`modules/cplugapi/errors.py`)

#### Added — WebSocket auth invariant shim (W2)

Pure-ASGI middleware that 403s WebSocket upgrades under
`/cplugapi/v1/*` when `--api-auth` is configured and the upgrade
lacks valid Basic credentials. No WS endpoints exist today; the
shim is forward-checked so a future T31 (`/session/stream/...`)
can't silently regress invariant 4. Capability:
`security/ws-auth-enforced`.
(`modules/cplugapi/ws_auth.py`)

#### Added — token-bucket rate limiting (W8)

Three classes (`mutating` POST/PUT/PATCH/DELETE, `read`
GET/HEAD/OPTIONS, `auth_failed` 401 observability). Off by default
in `desktop` profile; cloud profile defaults
`mutating=30/min`/`read=600/min`/`auth_failed=10/min`. 429
responses use the Problem Details envelope plus standard
`Retry-After`. Every successful response carries
`X-RateLimit-Limit`/`-Remaining`/`-Reset`. Cloud profile requires
`CPLUG_TRUSTED_PROXIES` for safe XFF parsing — fail-fast at
startup. Per-credential keying on `username`-only (not
`username + password-prefix`, which would let brute-forcers
bypass). Capability: `security/rate-limit`.
(`modules/cplugapi/rate_limit.py`)

#### Added — Prometheus / OpenMetrics endpoint (W10)

`GET /cplugapi/v1/metrics` returns text/plain;version=0.0.4 with
`cplugapi_requests_total{method,path,status}` (counter),
`cplugapi_request_duration_seconds{method,path}` (histogram),
`cplugapi_idempotency_replays_total`,
`cplugapi_active_task_id_present`. Vendored 80-LoC formatter — no
`prometheus_client` dep. Cardinality cap of 100 distinct paths
(overflow bucketed as `<other>`). Integrated via
`logging.Handler` attached to `cplugapi.access` so `access_log.py`
stays untouched. Auth-gated by default; `CPLUG_METRICS_PUBLIC=1`
moves the endpoint to the public router for sidecar-style
scraping. Capability: `observability/metrics`.
(`modules/cplugapi/metrics.py`)

#### Added — W3C Trace Context propagation (W11)

Pure-ASGI middleware that parses inbound `traceparent`, validates
per W3C spec (all-zero trace-id/parent-id rejected), generates one
if absent/malformed, stashes on `request.state.traceparent` /
`request.state.trace_id`, and echoes on the response. OpenTelemetry
SDK integration deferred to a future `observability/trace-context-w3c-spans`
capability when the SDK is present. Access-log emits `traceparent`
and `trace_id` as structured-extra fields (visible when
`CPLUG_LOG_FORMAT=json` is set; see W9). Capability:
`observability/trace-context-w3c`.
(`modules/cplugapi/tracing.py`)

#### Added — graceful shutdown (W12)

SIGTERM bridges to an async shutdown sequence:

1. `livez_readyz.set_draining(True)` — drain flag visible on the
   public `/readyz` body (`checks.draining: true`).
2. Poll `progress.current_task` and `progress.pending_tasks` for
   up to `CPLUG_SHUTDOWN_GRACE_S` (default 30).
3. After grace expires, fire `shared.state.interrupt()` to abort
   stragglers.

Optional reject-during-drain middleware (`CPLUG_SHUTDOWN_REJECT_NEW=1`
or cloud profile default) returns 503 to new POSTs against
cplugapi and `/sdapi/v1/{txt2img,img2img}` during drain. Reads
pass through. Uses Starlette lifespan semantics via signal-handler
bridge (not deprecated `@app.on_event("shutdown")`). Capability:
`ops/graceful-shutdown`.
(`modules/cplugapi/shutdown.py`)

#### Added — deployment profile (W5)

`CPLUG_DEPLOYMENT_PROFILE=desktop|cloud` (default `desktop`).
Profile flips coordinated defaults:

| Knob | desktop | cloud |
|---|---|---|
| `CPLUG_ALLOWED_HOSTS` | loopback | `*` |
| `CPLUG_ALLOWED_ORIGINS` | loopback regex | `*` |
| auto_preempt mode | `always` | `off` |
| rate-limit classes | off | on (30/600/10) |
| reject-during-drain | off | on |

Explicit env vars override profile defaults. Capability
`deployment-profile-cloud` registered only when active.
(`modules/cplugapi/profile.py`)

#### Added — structured JSON logging mode (W9)

`CPLUG_LOG_FORMAT=json` swaps the formatter on every cplugapi-owned
logger (`cplugapi.access`, `.sdapi`, `.gen_timing`, `.upscale`,
`.preempt`, `.ws_auth`) for a stdlib-only JSON-line formatter
emitting `{ts, level, logger, msg, ...extra}` per record.
Un-jsonable `extra` values are repr'd, not raised. Capability:
`observability/log-format-json` (only when active).
(`modules/cplugapi/log_format.py`)

#### Added — `capabilities[]` on `/identify` (W4)

The unauthenticated `/identify` probe now surfaces the same
capability list as `/health` (filtered through a forward-guard
predicate that strips anything matching a 7-40-char hex SHA or a
checkpoint-file extension). Clients can negotiate features without
sending credentials. Same response also includes
`deprecated_capabilities[]` per W15.
(`modules/cplugapi/identify.py`)

#### Added — per-route body-size limits (W7)

`security_middleware` enforces a route-prefix table that overrides
the global 32 MiB cap on tiny endpoints:

- `POST /forge/preset/...` — 4 KiB
- `POST /session/cancel/...` — 4 KiB
- `POST /session/preempt` — 4 KiB

Matcher uses longest-prefix-with-`/`-or-EOS termination so an
adjacent path like `/forge/preset-bulk` correctly falls back to
the global cap. Env override `CPLUG_ROUTE_BODY_LIMITS=METHOD:path:bytes,...`.
Capability: `security/per-route-body-limits`.

#### Added — idempotency replay header allow-list (W6)

`Idempotency-Key` replay path swapped from a deny-list to an
explicit allow-list (`Content-Type`, `Cache-Control`, `ETag`,
`X-Cplug-*` prefix). Drops `Set-Cookie`, `Date`, `Server`,
`X-Request-Id`, etc. from cached replays — defence-in-depth
against future endpoints that set those headers. Middleware-order
regression test pins the canonical install order so a future
rebase can't silently regress correlation hygiene.

#### Added — fork-local capability namespacing with dual emission (W15)

Six fork-local capability strings now dual-emit a namespaced new
name plus the legacy flat name:

| Legacy (deprecated) | New |
|---|---|
| `request-log` | `observability/request-log` |
| `gen-timing` | `observability/gen-timing` |
| `sdapi-request-log` | `observability/sdapi-request-log` |
| `upscale-log` | `observability/upscale-log` |
| `livez` | `health/livez` |
| `readyz` | `health/readyz` |

Canonical strings (`session/cancel`, `forge/preset`,
`models/architecture`, etc.) are NOT renamed. `/health` and
`/identify` surface a new `deprecated_capabilities[]` array
listing the legacy names for one minor release; removal is
triggered by Rust client confirmation, not just elapsed time.

#### Changed — `/livez` and `/readyz` move to public router (W1)

K8s probes work without injecting Basic auth. Default `/readyz`
body is sanitised for unauthenticated callers (booleans only:
`torch_importable`, `model_loaded`, `has_error`, `draining`).
`?verbose=1` adds the full `last_error` record but requires
Basic auth when `--api-auth` is configured. `/livez` is
unconditional 200. Capability strings unchanged.

#### Changed — error response shape across cplugapi

Every error response uses RFC 9457 problem+json (see W3 above).
The legacy top-level `detail` field is kept populated alongside
the new structured envelope through one minor release of dual
emission, then removed.

#### Changed — CI publishes OpenAPI artifact on tag push (W18)

`.github/workflows/cplugapi-tests.yml` already uploaded
`cplugapi-openapi.json` as a workflow artifact on every PR; a new
`publish-openapi-on-tag` job attaches it to GitHub Releases on
tag push for the Rust client team to pin against a stable URL.

#### Documentation

- New `doc/cplugapi-threat-model.md` — threat model + mitigations
  + accepted risks (W19).
- New `doc/cplugapi-cloud-deploy.md` — cloud deployment runbook
  with k8s manifest sample, Prometheus scrape config, Loki/ELK
  ingestion (W20).
- `doc/cplugapi.md` — error code catalog (W17 — codes themselves
  landed with W3), middleware pattern explainer (W14), OpenAPI
  artifact section (W18), deployment profile table, per-endpoint
  examples (W21).

### Added — tagged upscale-request log

`POST /sdapi/v1/extra-single-image` and `POST /sdapi/v1/img2img`
carrying `X-Cplug-Intent: upscale` (or `upscale-img2img` /
`upscale-refine`) now emit a tagged INFO line on the
`cplugapi.upscale` logger:

```
upscale request: type=extras POST /sdapi/v1/extra-single-image in=128456
upscale request: type=img2img-refine POST /sdapi/v1/img2img in=2048576
```

Pure-ASGI middleware, sniffs path + headers only (no body reads).
`/sdapi/v1/img2img` without the intent header is silent — avoids
mistagging every sketch stroke as an upscale. Default ON
(`CPLUG_UPSCALE_LOG=0` to disable; upscale events are infrequent
enough that the line doesn't flood). Capability: `upscale-log`.
Frontend integration: client adds the header on its Img2Img-refine
upscale path; Extras flow needs no client change. See `doc/cplugapi.md`
"Upscale request log" for the full field reference.
(`modules/cplugapi/upscale_log.py`)

### Added — ControlNet patcher cache

`backend/patcher/controlnet.py:apply_controlnet_advanced` produces a
fresh `UnetPatcher` clone every gen (`m = unet.clone()` →
`m.add_patched_controlnet(cnet)`). Forge's `LoadedModel.__eq__`
compares patcher *identity*, so the lookup at
`memory_management.py:629` misses unconditionally and the
clone-cleanup path at `:642-650` runs every gen — detach the old
patcher's hooks, reattach the new ones. The underlying weights
never leave VRAM, but the walk costs ~1 s of "Moving model(s) has
taken X seconds" per gen on the test rig (Illustrious-XL + Xinsir
Union ProMax + 24 GB card under `--highvram`). Also generates the
spammy `Reusing ControlNet Model… / Requested to load … / loaded
completely` triplet on every gen.

`modules/cplugapi/controlnet_cache.py` monkey-patches the upstream
function (same pattern as `memmgmt_patches.py`) to cache the cloned
UNet patcher keyed on `(id(unet_baseline), id(controlnet_baseline))`.
On cache hit, the per-gen state (cond_hint, strength, percentages,
the five `advanced_*_weighting` attrs, control_type) is mutated
directly on the already-attached cnet — patcher identity stays
stable across gens, the equality lookup hits, the clone-cleanup
walk and its log spam don't fire.

ID reuse after GC is handled via weakref guards on the baselines.
Cache is bounded at 16 entries (FIFO). Disable via
`CPLUG_CONTROLNET_CACHE=0` for passthrough. Capability:
`controlnet/patcher-cache`.

We initially considered broadening `LoadedModel.__eq__` to compare
underlying-model identity, which would have fixed ToMe + ControlNet
+ any future patcher class in one shot. Audit found a correctness
break — the new patcher's per-gen state would be silently dropped
in favor of the old patcher's stale state. See
`devlog/2026-05-09-controlnet-patcher-cache.md` for the full
trace and rollback story.
(`modules/cplugapi/controlnet_cache.py`,
`modules/cplugapi/runtime.py`)

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
