# syntax=docker/dockerfile:1
# cplug_webui (ControlPlugin backend, fork of Forge Neo) | NVIDIA GPU
#
# Build context MUST be the repository root so the fork's own source is
# copied into the image (NOT cloned from upstream):
#
#     docker build -f docker/Dockerfile -t cplug_webui .
#
# Paths in docker/.dockerignore are repo-root-relative for that reason.

ARG CUDA_VERSION=12.6.3

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04

# See: https://download.pytorch.org/whl/
ARG TORCH_INDEX=cu126

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=automatic \
    UV_NO_CACHE=1 \
    TORCH_INDEX_URL=https://download.pytorch.org/whl/${TORCH_INDEX}

# uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ffmpeg \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libtcmalloc-minimal4 \
    && rm -rf /var/lib/apt/lists/*

# Non-Root User (UID 99 / GID 100 = nobody:users)
RUN groupadd -g 100 users 2>/dev/null || true \
    && useradd -u 99 -g 100 -d /home/forge -m -s /bin/bash forge

WORKDIR /home/forge/sd-webui

# Own the WebUI dir before dropping privileges so the venv + source land
# writable by the non-root runtime user.
RUN chown -R 99:100 /home/forge

USER forge

# Python 3.13 Virtual ENVironment — built before the source COPY so a
# source-only change does not bust the (slow) PyTorch install layer.
RUN uv venv venv --python 3.13 --seed

ENV VIRTUAL_ENV="/home/forge/sd-webui/venv" \
    PATH="/home/forge/sd-webui/venv/bin:$PATH" \
    PYTHON="/home/forge/sd-webui/venv/bin/python3.13"

# PyTorch Pre-Install
RUN uv pip install torch torchvision \
    --index-url "https://download.pytorch.org/whl/${TORCH_INDEX}"

# Copy the fork's own source into the image (build context = repo root).
# This is the fork, not an upstream clone — see header. .dockerignore
# (repo-root-relative) keeps .git / models / local state out of the
# context; venv/ is excluded too so this never clobbers the layer above.
COPY --chown=99:100 . /home/forge/sd-webui

# Persistent Data Directories (idempotent if already present in source)
RUN mkdir -p \
        models/Stable-diffusion \
        models/VAE \
        models/Lora \
        models/ControlNet \
        extensions \
        config \
        output

# Runtime — entrypoint arrived via the repo-root COPY at
# docker/entrypoint.sh; promote it to the WebUI root and mark executable.
RUN cp /home/forge/sd-webui/docker/entrypoint.sh /home/forge/sd-webui/entrypoint.sh \
    && chmod +x /home/forge/sd-webui/entrypoint.sh

EXPOSE 7860

# extra time for prepare_environment() to run on first start.
# Probe the fork's auth-exempt liveness route (200 unconditional, no UI
# dependency) rather than the Gradio root. /cplugapi/v1/livez is mounted
# on the public router so it answers without --api-auth credentials.
HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=15 \
    CMD curl -f http://localhost:7860/cplugapi/v1/livez || exit 1

ENTRYPOINT ["/home/forge/sd-webui/entrypoint.sh"]
