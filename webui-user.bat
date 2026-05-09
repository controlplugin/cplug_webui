@echo off

:: set PYTHON=
:: set GIT=
:: set VENV_DIR=

set COMMANDLINE_ARGS=--api --xformers --uv --cuda-malloc --highvram

:: --api                                                — required: exposes /sdapi/v1/* + /cplugapi/v1/* for the ControlPlugin client.
:: --xformers                                           — safe acceleration; broad GPU coverage. Mutually exclusive with --sage.
:: --uv                                                 — route pip through uv; faster cold installs (after `uv venv venv --python 3.13 --seed`).
:: --highvram                                           — keeps models in VRAM after use; eliminates eviction-driven reload latency between gens. Safe for 12 GB+ cards.
:: --gpu-only                                           — alternative to --highvram: pin EVERYTHING to GPU (no offload at all). 16 GB+ recommended.
:: --sage                                               — SageAttention 2++ (faster than xformers on Ada/Ampere; broken on RTX 2060 — see upstream #1036).
:: --pin-shared-memory --cuda-malloc --cuda-stream      — memory-locality trio. ~1-2 s/gen win on 12 GB+ cards; may OOM on smaller.
:: --api-auth user:password                             — Basic auth on /sdapi + /cplugapi. Recommended for any non-isolated machine.
:: --skip-python-version-check --skip-torch-cuda-test --skip-version-check --skip-prepare-environment --skip-install
::                                                      — only after a known-good launch; trims ~5-10 s off restarts.

:: cplugapi diagnostic logs — off by default in this fork. The desktop client polls progress at ~4 Hz which floods the
:: console when these are on. Flip any of them to 1 (or remove the line) to re-enable while diagnosing client behaviour.
set CPLUG_ACCESS_LOG=0
set CPLUG_SDAPI_OBSERVER=0
set CPLUG_GEN_TIMING=0
:: CPLUG_ACCESS_LOG     — one structured line per /cplugapi/v1/* request (method, path, status, dur_ms, request_id).
:: CPLUG_SDAPI_OBSERVER — one line per /sdapi/v1/* request (the upstream surface the client actually hits for gens).
:: CPLUG_GEN_TIMING     — one line per process_images_inner call (total_ms, vae_decode_ms, peak_vram_mb).
:: See doc/cplugapi.md "Diagnostic logging" for the full field reference.

:: Auto-preempt mode for /sdapi/v1/{txt2img,img2img}. Default is "always" — every gen submission cancels the running gen
:: + drains the pending queue so rapid sketch strokes don't stack up behind one another. Set to "header" to opt-in per
:: request (X-Cplug-Preempt: 1), or "off" to disable entirely. See doc/cplugapi.md "Auto-preempt".
:: set CPLUG_PREEMPT_MODE=always

call webui.bat
