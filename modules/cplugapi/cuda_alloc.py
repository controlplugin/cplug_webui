"""CUDA allocator tweaks for live-sketching workloads (audit 02 Phase C).

Mitigates upstream issue Forge-Neo #1049 (fragmentation-driven OOM
under repeated checkpoint swaps on Linux + CUDA). The fix is to flip
the PyTorch allocator into ``expandable_segments`` mode, which lets
freed regions coalesce instead of stranding pinned virtual addresses.

The env var ``PYTORCH_CUDA_ALLOC_CONF`` must be set *before* any
CUDA context is created, so :func:`configure_expandable_segments` is
intended to be called at module-import time from the cplugapi
bootstrap path — not from inside a request handler.

Constraints:

* Linux-only — Windows ships a different allocator backend that
  ignores the flag.
* No-op if the operator has already set ``PYTORCH_CUDA_ALLOC_CONF``
  (their override wins).
* No-op if torch is not importable or CUDA is not visible (CPU-only
  builds, MPS, ROCm-not-detected, …).
"""

from __future__ import annotations

import logging
import os
import sys

_log = logging.getLogger(__name__)

_ENV_KEY = "PYTORCH_CUDA_ALLOC_CONF"
_VALUE = "expandable_segments:True"

_APPLIED_THIS_PROCESS = False


def _cuda_looks_available() -> bool:
    """Best-effort probe — returns True if torch is importable AND
    reports a CUDA-capable build.

    We deliberately do *not* call ``torch.cuda.is_available()`` here
    because that initializes the CUDA context, which would defeat the
    whole purpose of setting the allocator env var in the first place.
    """
    try:
        import torch
    except ImportError:
        return False
    # ``torch.version.cuda`` is the build-time CUDA version string
    # ("12.1", "11.8", …) on CUDA wheels and ``None`` on CPU-only
    # wheels. Reading it does not initialize the runtime.
    return getattr(getattr(torch, "version", None), "cuda", None) is not None


def configure_expandable_segments() -> bool:
    """Set ``PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`` early.

    Returns True when the env var was newly applied by this call,
    False otherwise (already set, non-Linux, no CUDA build).
    """
    global _APPLIED_THIS_PROCESS
    if os.environ.get(_ENV_KEY):
        return False
    if not sys.platform.startswith("linux"):
        return False
    if not _cuda_looks_available():
        return False
    os.environ[_ENV_KEY] = _VALUE
    _APPLIED_THIS_PROCESS = True
    _log.info(
        "cplugapi: %s=%s applied (mitigates upstream Forge-Neo #1049)",
        _ENV_KEY,
        _VALUE,
    )
    return True


def expandable_segments_active() -> bool:
    """Predicate for the ``runtime/expandable-segments`` capability.

    True if either this process applied the flag or the operator had
    already set ``PYTORCH_CUDA_ALLOC_CONF`` to a value that contains
    ``expandable_segments:True``. The latter check tolerates the
    standard comma-delimited multi-option form
    (``max_split_size_mb:128,expandable_segments:True``).
    """
    if _APPLIED_THIS_PROCESS:
        return True
    val = os.environ.get(_ENV_KEY, "")
    return "expandable_segments:true" in val.lower()


def register_capabilities() -> None:
    """Register the ``runtime/expandable-segments`` capability."""
    from modules.cplugapi import capabilities

    capabilities.register(
        "runtime/expandable-segments",
        predicate=expandable_segments_active,
    )
