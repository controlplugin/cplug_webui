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
import threading
import time
from typing import Any, Optional

from . import capabilities

_log = logging.getLogger("cplugapi.gen_timing")

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


def _start_gen_record() -> dict:
    """Initialise a fresh per-gen timing dict and bind it to the context.

    Returns the dict so the wrapper can read ``start`` from it.
    """
    record = {"start": time.perf_counter(), "stages": {}}
    return record


def _emit(record: dict, error: Optional[str] = None) -> None:
    """Format + log one structured line for the completed gen."""
    total_ms = (time.perf_counter() - record["start"]) * 1000.0
    stages: dict[str, float] = record["stages"]

    extra: dict[str, Any] = {
        "total_ms": round(total_ms, 1),
        **{f"{name}_ms": round(value, 1) for name, value in stages.items()},
    }
    if error is not None:
        extra["error"] = error

    rendered_stages = " ".join(
        f"{name}_ms={value:.1f}" for name, value in stages.items()
    )
    suffix = f" {rendered_stages}" if rendered_stages else ""
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
    """Wrap the upstream gen pipeline functions. Idempotent."""
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
    """Advertise per-gen pipeline timing."""
    capabilities.register("gen-timing")
