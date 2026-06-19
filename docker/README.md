<h2 align="center">cplug_webui (Docker)</h2>

Headless API backend for the **ControlPlugin** Photoshop plugin — a fork of
Stable Diffusion WebUI Forge Neo. This image runs the fork's own source and
exposes both the upstream `/sdapi/v1/*` surface and the fork-specific
`/cplugapi/v1/*` surface.

> [!Warning]
> Requires an **NVIDIA** GPU. Ensure the driver is up to date (`560+` required).

<hr>

## Building

The build context **must be the repository root** so the fork's source is
copied into the image (it is *not* cloned from upstream). Run from the repo
root, pointing `-f` at this Dockerfile:

```bash
docker build -f docker/Dockerfile -t cplug_webui .
```

`docker/.dockerignore` lists repo-root-relative excludes (`.git`, `venv/`,
`models/`, weights, `plan/`, caches, local state) so the image stays lean and
never bakes in local models or local state.

<hr>

## Running

This is a headless API backend; the entrypoint always launches with
`--listen --api`. Supply your Basic-auth credential via `COMMANDLINE_ARGS`
(or as trailing arguments) so it stays out of the image:

```bash
docker run --gpus all -p 7860:7860 \
    -e COMMANDLINE_ARGS="--api-auth user:CHANGE_ME" \
    -v /path/to/models:/home/forge/sd-webui/models \
    -v /path/to/output:/home/forge/sd-webui/output \
    -v /path/to/config:/home/forge/sd-webui/config \
    cplug_webui
```

`--api-auth` makes `/cplugapi/v1/*` inherit the same Basic auth as
`/sdapi/v1/*` — there is no second auth layer.

<hr>

## Health probes

| Route | Auth | Purpose |
| --- | --- | --- |
| `/cplugapi/v1/livez` | **exempt** | 200 unconditionally — liveness. The Docker `HEALTHCHECK` uses this. |
| `/cplugapi/v1/readyz` | exempt (booleans only); `?verbose=1` requires creds | Readiness — 200 when torch is importable, a checkpoint is loaded, no fatal error, not draining; 503 otherwise. |
| `/cplugapi/v1/health` | requires `--api-auth` creds | Full health + capability registry. |

The container `HEALTHCHECK` polls `/cplugapi/v1/livez`, which is mounted on the
public router and answers without credentials — it is a real readiness signal
that does not depend on the Gradio UI.

<hr>

## TLS reverse proxy (recommended)

`--api-auth` is HTTP Basic auth: credentials are base64-encoded, **not
encrypted**. They are only meaningfully protected over HTTPS. Put a TLS
terminating reverse proxy (nginx, Caddy, Traefik, a cloud load balancer) in
front of this container and never expose port 7860 directly to an untrusted
network.

<hr>

## Persistent data

Bind-mount these container paths so models, output, and settings survive
container recreation:

| Container path | Purpose |
| --- | --- |
| `/home/forge/sd-webui/models` | Checkpoints, VAE, LoRA, ControlNet |
| `/home/forge/sd-webui/output` | Generated images |
| `/home/forge/sd-webui/extensions` | User-installed extensions |
| `/home/forge/sd-webui/config` | User settings (`config.json`, `ui-config.json`, …) |

The container runs as **UID 99 / GID 100** (`nobody:users`). Ensure the
bind-mounted host directories are writable by that uid/gid.

<hr>

## Image details

| | |
| --- | --- |
| Base | `nvidia/cuda:12.6.3-runtime-ubuntu22.04` |
| Python | `3.13` via **uv** |
| PyTorch | Latest (`cu126`) |
| User | `forge` (UID 99 / GID 100) |
| Port | 7860 |

> [!Note]
> On the first run, `prepare_environment()` installs requirements and
> dependencies. This may take a few minutes; the `HEALTHCHECK` allows a
> generous start period to cover it.
