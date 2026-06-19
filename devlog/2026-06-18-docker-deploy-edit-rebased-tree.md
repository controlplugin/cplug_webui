# 2026-06-18 — docker/ tree adapted for the fork

**Symptom.** The `docker/` tree arrived from upstream via the neo merge and was
wrong for this fork in two load-bearing ways:
1. `Dockerfile` did `git clone --branch neo Haoming02/sd-webui-forge-classic` —
   the image shipped UPSTREAM's code, not the fork.
2. `HEALTHCHECK` probed `http://localhost:7860/` (the Gradio root), which needs
   the web UI and isn't a real readiness signal for a headless API server.

**Decision.**
- **Build from the fork's own source.** Replaced the upstream clone with
  `COPY --chown=99:100 . /home/forge/sd-webui` (build context = repo root;
  `docker build -f docker/Dockerfile -t cplug_webui .`). Reordered so the uv
  venv + PyTorch install run BEFORE the source COPY (a source edit no longer
  busts the torch layer). Preserved the non-root user (uid 99 / gid 100), uv
  venv, ownership, and persistent-dir setup.
- **Auth-exempt healthcheck.** `CMD curl -f http://localhost:7860/cplugapi/v1/livez`.
  Verified against `modules/cplugapi/livez_readyz.py`: `/livez` returns 200
  unconditionally and is attached to the PUBLIC (auth-exempt) router, unlike
  `/health` (private/auth-gated) and `/readyz?verbose` (creds for detail).
- **Entrypoint.** Added `--api` alongside `--listen`; `--api-auth` is passed at
  run time (kept out of the image as a secret). Confirmed the merge-removed
  flags (`--use-cpu`, old `--sage2-*`) are gone and never referenced; the uv
  `input()` prompt is gated behind `isatty()`/CI so a non-interactive container
  never blocks.
- **New `docker/.dockerignore`** (repo-root-relative) excludes `.git`, `venv/`,
  `models/` + weight files, `outputs/`, `plan/`, `.uv-cache/`, local state
  (config.json, ui-config.json, …), and IDE/OS junk — so the image stays lean
  and never bakes in local models or developer state.
- **docker/README.md** rewritten for cplug_webui: correct build command, a
  probe table (livez auth-exempt vs health/readyz needing creds), and a TLS
  reverse-proxy recommendation (Basic auth is only meaningful over HTTPS). One
  pointer line added to the top-level README.

**Alternatives considered.** Cloning the fork's public origin instead of COPY
(rejected: needs network at build + assumes a public HTTPS URL; COPY builds
exactly what's checked out).

**Blast radius.** `docker/` only (+ a one-line README pointer). No application
code; no `/sdapi` surface.

**Failure modes.** Forgetting to build from repo root → COPY fails (documented).
A missing `.dockerignore` would bake in `venv/`/`models/` → covered.

**Verification.** `bash -n docker/entrypoint.sh` clean; greps confirm the
upstream clone is gone and the healthcheck hits `/cplugapi/v1/livez`; leak scrub
clean (only intentional container-internal `localhost:7860` + the image's fixed
`/home/forge` user remain). A real `docker build` needs a Docker host.

**Rollback.** Revert this commit; restores the upstream-delivered docker tree.
