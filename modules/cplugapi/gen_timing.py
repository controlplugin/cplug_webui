"""Per-generation pipeline timing for ``/sdapi/v1/{txt2img,img2img}``.

Why this exists: the cplugapi access log times the HTTP envelope, but
the actual gen runs through ``modules.processing.process_images_inner``
on a worker thread spawned by upstream code. To diagnose "where did
the 20s go" we want a single line per gen with a stage breakdown:

  gen total_ms=8412 vae_decode_ms=287 ...

The hook is an in-place wrap of upstream functions, installed at
``setup_cplugapi`` time. Wrapping rather than copying:

- ``modules.processing.process_images_inner`` — top-level. Fires the
  log line on exit, including the exception path so failed gens are
  visible too.
- ``modules.processing.decode_latent_batch`` — VAE decode. Adds
  ``vae_decode_ms`` to the active gen's stage dict.

The active gen is tracked via :class:`contextvars.ContextVar` so a
high-res-fix pass (which calls ``decode_latent_batch`` from inside
``process_images_inner``) accumulates into the same record. Nested or
parallel gens each get their own copy via the contextvar's natural
isolation.

**Read-only**: the wrappers measure but never mutate the response. A
client that ignores fork-specific log output sees byte-identical
behavior to upstream — preserves the ``/sdapi/v1/*`` invariant
(CLAUDE.md §1).

**Idempotent install**: the patches are stamped onto the module
under fork-prefixed attribute names so a second install is a no-op
rather than a double-wrap (which would multiply call counts by N).
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from typing import Any, Optional

from . import capabilities

_log = logging.getLogger("cplugapi.gen_timing")
try:
    # Forge's logging helper installs a Rich console handler at the
    # configured log level. Without it our INFO lines are swallowed by
    # the default root config.
    from backend.logging import setup_logger as _setup_logger
    _setup_logger(_log)
except ImportError:
    pass  # OpenAPI export / tests stub backend out

# Env-var kill switch — read once at install time. Disabling skips
# emission only; the wrap is still installed so the contextvar
# infrastructure stays consistent and other observers (e.g. tests)
# can still introspect timings if they patch ``_emit`` directly.
_ENV_DISABLE = "CPLUG_GEN_TIMING"


def _is_enabled() -> bool:
    raw = os.environ.get(_ENV_DISABLE)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# Captured at install time so the runtime hot path doesn't pay an
# env-var lookup per gen.
_emission_enabled: bool = True

# Per-gen timing context. ContextVar is the right primitive because
# Forge dispatches gens onto worker threads; a thread-local would also
# work but contextvar plays nicely with asyncio handlers if the future
# pipeline ever migrates that way.
_current_timing: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "_cplug_gen_timing", default=None,
)

# Module-level guard so install_hooks is idempotent. Stamped onto the
# upstream module so a webui reload (which re-imports cplugapi) still
# sees the patch and short-circuits cleanly.
_INSTALL_FLAG = "_cplug_gen_timing_installed"
_install_lock = threading.Lock()


def _reset_peak_vram() -> None:
    """Reset the CUDA peak-allocated counter so the next gen records its
    own watermark, not the running max-of-all-time. Best-effort: torch
    may be unavailable (test env) or CUDA may not be active (CPU mode);
    in both cases the call is a silent no-op so timing still works.
    """
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def _peak_vram_mb() -> Optional[float]:
    """Return peak VRAM allocated since the last reset, in MiB.

    Diagnostic for "is CUDA Sysmem Fallback firing?" — if peak hits
    near total VRAM during sampling, the NVIDIA driver is silently
    spilling tensors over PCIe to host RAM, and the iteration time
    will be 10-20x slower than expected. ``None`` when CUDA is not
    available so log readers can distinguish "not measured" from "0".
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return torch.cuda.max_memory_allocated() / (1024 * 1024)
    except Exception:
        return None


def _start_gen_record() -> dict:
    """Initialise a fresh per-gen timing dict and bind it to the context.

    Returns the dict so the wrapper can read ``start`` from it.
    """
    _reset_peak_vram()
    record = {"start": time.perf_counter(), "stages": {}}
    return record


def _emit(record: dict, error: Optional[str] = None) -> None:
    """Format + log one structured line for the completed gen.

    No-op when emission is disabled via ``CPLUG_GEN_TIMING=0``. The
    rest of the gen-timing infrastructure (contextvar, stage tracking,
    peak-VRAM reset) stays live so a future toggle-on doesn't require
    restart — though the env var itself is read once at install.
    """
    if not _emission_enabled:
        return
    total_ms = (time.perf_counter() - record["start"]) * 1000.0
    stages: dict[str, float] = record["stages"]
    peak_vram = _peak_vram_mb()

    extra: dict[str, Any] = {
        "total_ms": round(total_ms, 1),
        **{f"{name}_ms": round(value, 1) for name, value in stages.items()},
    }
    if peak_vram is not None:
        extra["peak_vram_mb"] = round(peak_vram, 1)
    if error is not None:
        extra["error"] = error

    rendered_stages = " ".join(
        f"{name}_ms={value:.1f}" for name, value in stages.items()
    )
    suffix = f" {rendered_stages}" if rendered_stages else ""
    if peak_vram is not None:
        suffix += f" peak_vram_mb={peak_vram:.1f}"
    if error is not None:
        suffix += f" error={error}"

    _log.info(f"gen total_ms={total_ms:.1f}{suffix}", extra=extra)


def _record_stage(name: str, duration_ms: float) -> None:
    """Add ``duration_ms`` to the named stage of the current gen.

    Multiple invocations of the same stage (e.g., HR pass calling
    ``decode_latent_batch`` twice) accumulate so the line reflects
    total time spent there.
    """
    record = _current_timing.get()
    if record is None:
        return
    stages = record["stages"]
    stages[name] = stages.get(name, 0.0) + duration_ms


def install_hooks() -> None:
    """Wrap the upstream gen pipeline functions. Idempotent.

    Snapshots the env-var-driven enable flag at install time so the
    hot path doesn't pay an env lookup per gen.
    """
    global _emission_enabled
    _emission_enabled = _is_enabled()
    with _install_lock:
        try:
            from modules import processing as _proc
        except ImportError:
            # The OpenAPI export script stubs out ``modules.processing``;
            # we shouldn't crash the cplugapi mount in that environment.
            return
        if getattr(_proc, _INSTALL_FLAG, False):
            return
        setattr(_proc, _INSTALL_FLAG, True)

        original_process = _proc.process_images_inner
        original_decode = _proc.decode_latent_batch

        def wrapped_process_images_inner(p, *args, **kwargs):
            record = _start_gen_record()
            token = _current_timing.set(record)
            error: Optional[str] = None
            try:
                return original_process(p, *args, **kwargs)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                _emit(record, error=error)
                _current_timing.reset(token)

        def wrapped_decode_latent_batch(*args, **kwargs):
            record = _current_timing.get()
            if record is None:
                # Outside a gen — could be a script or an extension
                # decoding latents on its own. Don't time it; skip.
                return original_decode(*args, **kwargs)
            start = time.perf_counter()
            try:
                return original_decode(*args, **kwargs)
            finally:
                _record_stage("vae_decode", (time.perf_counter() - start) * 1000.0)

        _proc.process_images_inner = wrapped_process_images_inner
        _proc.decode_latent_batch = wrapped_decode_latent_batch


def register_capabilities() -> None:
    """Advertise per-gen pipeline timing (only when enabled).

    W15 — dual-emits ``observability/gen-timing`` (new) and
    ``gen-timing`` (legacy)."""
    if _is_enabled():
        capabilities.register_with_legacy(
            new_name="observability/gen-timing",
            legacy_name="gen-timing",
        )
