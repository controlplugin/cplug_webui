"""Runtime tweaks applied when cplugapi is mounted.

These are fork-only behavior knobs that should default ON for the
ControlPlugin live-sketching workload but stay invisible to upstream
Forge Neo. Anything that mutates global PyTorch / cuDNN / allocator
state belongs here, not in ``backend/`` — it keeps the rebase surface
on upstream-shared files at zero.

Each tweak must be:

* idempotent (called every ``setup_cplugapi``);
* safe to skip on platforms where it does not apply (CPU-only, MPS, …);
* logged so an operator can correlate a behavior change with this hook.

The hook fires from :func:`modules.cplugapi.router.setup_cplugapi` once
per FastAPI app instance.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_APPLIED = False


def apply_runtime_tweaks() -> None:
    """Apply fork-only runtime defaults. Idempotent across calls."""
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    _enable_cudnn_benchmark()


def _enable_cudnn_benchmark() -> None:
    """Default cudnn.benchmark ON (audit 01 §3.1).

    The live-sketching loop is the canonical fixed-shape, fixed-model
    workload that cudnn.benchmark was designed for. Upstream gates this
    behind ``--autotune``; the fork flips the default because every
    cplugapi session pays the same cost/benefit.

    No-op on non-CUDA builds and on PyTorch installs without cuDNN.
    """
    try:
        import torch
    except ImportError:
        return
    if not torch.cuda.is_available():
        return
    if not torch.backends.cudnn.is_available():
        return
    if torch.backends.cudnn.benchmark:
        return
    torch.backends.cudnn.benchmark = True
    _log.info("cplugapi: cudnn.benchmark enabled (audit 01 §3.1)")


def apply_channels_last(diffusion_model) -> None:
    """Convert a convolutional UNet to channels_last memory format
    (audit 01 §3.2).

    Engines for convolutional architectures (SD1.5 / SDXL / SDXLRefiner)
    call this once with their finalized ``unet.model.diffusion_model``;
    DiT engines (Flux / SD3 / Qwen / Lumina / …) skip it because the
    layout offers no benefit there.

    No-op when:

    * the user passed ``--no-channels-last``;
    * CUDA is unavailable or the active device is not Ampere+ (sm_80,
      where channels_last conv2d gets the actual speedup);
    * a torch import fails (defensive — the caller always has torch but
      this helper might be invoked from non-torch paths in tests).
    """
    try:
        import torch
        from backend.args import args
    except ImportError:
        return
    if getattr(args, "no_channels_last", False):
        return
    if not torch.cuda.is_available():
        return
    try:
        device = next(diffusion_model.parameters()).device
        if device.type != "cuda":
            return
        major = torch.cuda.get_device_properties(device).major
    except (StopIteration, AttributeError, RuntimeError):
        return
    if major < 8:
        return
    diffusion_model.to(memory_format=torch.channels_last)
    _log.info("cplugapi: channels_last applied to %s (audit 01 §3.2)", type(diffusion_model).__name__)
