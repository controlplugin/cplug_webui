@echo off

:: set PYTHON=
:: set GIT=
:: set VENV_DIR=

set COMMANDLINE_ARGS=--api --xformers --uv --cuda-malloc

:: --api                                                — required: exposes /sdapi/v1/* + /cplugapi/v1/* for the ControlPlugin client.
:: --xformers                                           — safe acceleration; broad GPU coverage. Mutually exclusive with --sage.
:: --uv                                                 — route pip through uv; faster cold installs (after `uv venv venv --python 3.13 --seed`).
:: --sage                                               — SageAttention 2++ (faster than xformers on Ada/Ampere; broken on RTX 2060 — see upstream #1036).
:: --pin-shared-memory --cuda-malloc --cuda-stream      — memory-locality trio. ~1-2 s/gen win on 12 GB+ cards; may OOM on smaller.
:: --api-auth user:password                             — Basic auth on /sdapi + /cplugapi. Recommended for any non-isolated machine.
:: --skip-python-version-check --skip-torch-cuda-test --skip-version-check --skip-prepare-environment --skip-install
::                                                      — only after a known-good launch; trims ~5-10 s off restarts.

call webui.bat
