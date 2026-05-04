# Changes

Fork-specific changes only. Upstream Forge Neo changes flow in via rebase
and are not duplicated here. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely — chronological,
grouped by **Added / Changed / Fixed / Removed**.

## Unreleased

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
