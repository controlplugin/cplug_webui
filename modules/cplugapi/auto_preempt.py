"""Auto-preempt for ``/sdapi/v1/{txt2img,img2img}``.

Fires ``shared.state.interrupt()`` and clears the pending queue
**before forwarding** an incoming gen request to the upstream handler.
The new gen then waits ~1 sample step on Forge's ``queue_lock`` while
the cancelled gen exits, then proceeds normally.

Result: rapid strokes from the live-sketching desktop client stop
stacking up — each new gen submission instantly cancels its predecessor.

Three modes via the ``CPLUG_PREEMPT_MODE`` env var:

- ``always`` (default for this fork): every gen request preempts. Best
  for sketch workflows where the most recent stroke is always the one
  that matters.
- ``header``: only when ``X-Cplug-Preempt: 1`` (or ``true`` / ``yes`` /
  ``on``) is present on the request. Per-request opt-in for clients
  that want to mark some gens as terminal (final renders) and others
  as preemptive (live previews).
- ``off``: never preempt — pure passthrough, equivalent to upstream
  behavior. Use this if the fork's default conflicts with another
  workflow.

Why pure ASGI rather than ``BaseHTTPMiddleware``:

Same reason as :mod:`sdapi_observer` — ``BaseHTTPMiddleware`` wraps
responses through anyio task groups + a memory-channel buffer, which
deadlocks streaming responses (encode/starlette#1438). Pure ASGI
inspects scope (path + headers), does its work synchronously before
forwarding, and never touches ``send``.

Path-scoped to ``/sdapi/v1/txt2img`` and ``/sdapi/v1/img2img`` — the
two real generation entry points. ``/sdapi/v1/options``,
``/sdapi/v1/progress``, etc. don't trigger gens; preempting on those
would be ill-defined.

Read-only on the upstream surface: when no preempt fires, the
middleware is a straight pass-through. Preserves the ``/sdapi/v1/*``
byte-identity invariant (CLAUDE.md hard invariant 1).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from fastapi import FastAPI

from . import capabilities

_log = logging.getLogger("cplugapi.preempt")
try:
    from backend.logging import setup_logger as _setup_logger
    _setup_logger(_log)
except ImportError:
    pass

# Header name + truthy-value set. Per-request opt-in (or opt-out — the
# inverse header below) lets the client mark live-preview strokes as
# preemptive without affecting terminal final-render submissions.
HEADER_NAME = b"x-cplug-preempt"
_TRUTHY: frozenset[bytes] = frozenset((b"1", b"true", b"yes", b"on"))

# Mode env var. Read once at install time, cached per-instance — we
# don't want to re-parse the env on every gen request. Operators
# tweaking the mode need to restart the webui (same constraint as
# every other launcher / config option).
ENV_MODE = "CPLUG_PREEMPT_MODE"
MODE_ALWAYS = "always"
MODE_HEADER = "header"
MODE_OFF = "off"
DEFAULT_MODE = MODE_ALWAYS  # fork-specific: sketch workflows are the norm

_VALID_MODES: frozenset[str] = frozenset((MODE_ALWAYS, MODE_HEADER, MODE_OFF))

# Path scope. Both txt2img and img2img enter Forge's gen pipeline
# through ``process_images_inner`` and queue on the same lock. Other
# /sdapi/v1/ paths (options, progress, sd-models, ...) don't trigger
# generation and shouldn't be preempt-aware.
_GEN_PATHS: frozenset[str] = frozenset((
    "/sdapi/v1/txt2img",
    "/sdapi/v1/img2img",
))


def _resolve_mode() -> str:
    """Read ``CPLUG_PREEMPT_MODE`` once and validate.

    Falls back to :data:`DEFAULT_MODE` on missing or unrecognised
    values, with a warning so a typo doesn't silently produce
    unexpected behavior.
    """
    raw = os.environ.get(ENV_MODE, "").strip().lower()
    if raw == "":
        return DEFAULT_MODE
    if raw in _VALID_MODES:
        return raw
    _log.warning(
        "%s=%r not recognised; falling back to %r. "
        "Valid values: %s",
        ENV_MODE, raw, DEFAULT_MODE, sorted(_VALID_MODES),
    )
    return DEFAULT_MODE


def _has_preempt_header(scope) -> bool:
    """True if the request carries a truthy ``X-Cplug-Preempt`` header.

    ASGI scope headers are a list of (bytes, bytes) tuples normalised
    to lowercase by Starlette. We compare on bytes to avoid the cost
    of decoding every header for every gen request — comparison
    happens on the hot path.
    """
    for name, value in scope.get("headers", []):
        if name == HEADER_NAME:
            return value.strip().lower() in _TRUTHY
    return False


def _preempt_now() -> tuple[Optional[str], int]:
    """Cancel the running gen and drain the pending queue.

    Returns ``(preempted_task_id, cleared_pending_count)`` for log
    introspection. Mirrors :mod:`session_preempt` semantics:

    - Snapshot ``progress.current_task`` before doing any work — the
      response payload should reflect the state at entry, not after
      the interrupt has had time to propagate.
    - ``shared.state.interrupt()`` is global and cooperative; wrap it
      in try/except so a misbehaving handler never breaks the forward
      path. If it raises we still record cancellation in
      ``cancelled_tasks`` so late status pokes are coherent.
    - Drain ``pending_tasks`` over a snapshot of keys to avoid the
      ``RuntimeError: dictionary changed size during iteration`` race
      with concurrent submitters.
    - **Must NOT take queue_lock.** Holding it would deadlock behind
      the gen we're trying to interrupt.
    """
    from modules import progress, shared

    from . import cancelled_tasks

    current_at_entry = progress.current_task
    if current_at_entry is not None:
        # Re-read RIGHT before interrupt to narrow the residual race
        # where the running task may have changed between observation
        # and action. Sub-microsecond residual race is accepted.
        if progress.current_task == current_at_entry:
            try:
                shared.state.interrupt()
            except Exception:
                pass
            cancelled_tasks.add(current_at_entry)

    cleared = 0
    pending_keys = list(progress.pending_tasks.keys())
    for tid in pending_keys:
        if progress.pending_tasks.pop(tid, None) is not None:
            cancelled_tasks.add(tid)
            cleared += 1

    return current_at_entry, cleared


class CplugapiPreempt:
    """Pure-ASGI middleware that fires preempt on incoming gen requests.

    Behavior is mode-driven (see module docstring): ``always`` fires on
    every gen, ``header`` only when ``X-Cplug-Preempt`` is truthy,
    ``off`` is a pure passthrough.

    The middleware runs *before* the request hits Forge's handler, so
    by the time the handler tries to acquire ``queue_lock``, the
    previously-running gen has been told to stop. The lock is released
    when that gen notices ``state.interrupted`` and exits — typically
    one sample step (~80 ms). The new gen then proceeds normally.
    """

    def __init__(self, app, mode: str = DEFAULT_MODE) -> None:
        self.app = app
        self.mode = mode

    async def __call__(self, scope, receive, send):
        if self.mode == MODE_OFF:
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path not in _GEN_PATHS:
            await self.app(scope, receive, send)
            return

        # Decide whether this specific request triggers preempt.
        # ``always`` mode skips header inspection entirely.
        should_preempt = (
            self.mode == MODE_ALWAYS
            or (self.mode == MODE_HEADER and _has_preempt_header(scope))
        )
        if not should_preempt:
            await self.app(scope, receive, send)
            return

        try:
            preempted_id, cleared = _preempt_now()
        except Exception as exc:
            # Any failure inside the preempt logic must NOT break the
            # forward path — a sketch stroke that fails to cancel the
            # previous gen is annoying but recoverable; a stroke that
            # 500s is a worse user experience.
            _log.warning("preempt failed: %s", exc, exc_info=True)
        else:
            # Only log when we actually had something to cancel — a
            # cold preempt (no running task, empty queue) on every
            # ``always`` mode gen would flood the log.
            if preempted_id is not None or cleared > 0:
                _log.info(
                    "preempt fired: preempted=%s cleared_pending=%d mode=%s",
                    preempted_id or "none",
                    cleared,
                    self.mode,
                )

        await self.app(scope, receive, send)


_INSTALL_FLAG = "cplugapi_preempt_installed"
_install_lock = threading.Lock()


def install(app: FastAPI, mode: Optional[str] = None) -> None:
    """Attach the middleware to ``app``. Idempotent + thread-safe.

    ``mode`` overrides the env-var-resolved default when provided
    (test-only — production callers should let env-var resolution
    drive behavior so operators can tune without code changes).
    """
    from starlette.middleware import Middleware

    resolved_mode = mode if mode is not None else _resolve_mode()

    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(0, Middleware(CplugapiPreempt, mode=resolved_mode))
        setattr(app.state, _INSTALL_FLAG, True)
        _log.info("auto-preempt installed: mode=%s", resolved_mode)


def register_capabilities() -> None:
    """Advertise the auto-preempt mechanism + the active mode.

    Two capability strings: a base ``sdapi/preempt`` for "this build
    knows how to preempt at all", plus a mode-specific one so clients
    can detect which behavior is active without a round-trip to a
    config endpoint. Off-mode advertises neither (clients should
    treat that build as no-op upstream).
    """
    mode = _resolve_mode()
    if mode == MODE_OFF:
        return
    capabilities.register("sdapi/preempt")
    capabilities.register(f"sdapi/preempt-{mode}")


# ---------------------------------------------------------------------------
# Late-abort hook on process_images_inner
# ---------------------------------------------------------------------------
#
# The pre-handler middleware above is necessary but not sufficient. Forge's
# API handler structure is:
#
#     add_task_to_queue(task_id)         # joins pending_tasks
#     with self.queue_lock:               # blocks if held
#         shared.state.begin(...)         # ← RESETS self.interrupted = False
#         start_task(task_id)             # pops from pending, sets current
#         processed = process_images(p)   # actual gen
#         finish_task(task_id)
#
# Multiple in-flight strokes:
#  - Each stroke's middleware fires interrupt() and drains pending_tasks.
#    The drain marks every previously-queued task in ``cancelled_tasks``.
#  - But once a queued gen's handler acquires queue_lock and calls
#    ``state.begin()``, the interrupt flag is cleared. The gen then runs
#    to completion despite being marked cancelled — ``cancelled_tasks`` is
#    only a status-poke marker, it doesn't gate execution.
#
# Mitigation: wrap ``process_images_inner`` to re-arm ``state.interrupted``
# at entry if the active task (``progress.current_task`` — already set by
# ``start_task`` by the time we run) is in ``cancelled_tasks``. The next
# sample-step check exits immediately, ``process_images`` returns a
# near-empty ``Processed``, the handler completes normally and the client
# sees a fast empty response. Each preempted gen now spends ~100 ms on
# queue_lock contention + sampler init instead of running 13+ steps.


_HOOK_INSTALL_FLAG = "_cplug_auto_preempt_hook_installed"
_hook_install_lock = threading.Lock()


def install_hooks() -> None:
    """Wrap ``modules.processing.process_images_inner`` with the late-abort
    check. Idempotent — flag stamped on the upstream module so a webui
    reload doesn't double-wrap.

    Must run AFTER :func:`gen_timing.install_hooks` so the abort path is
    measured by gen_timing's wall-clock counter (preempted gens show up
    with very small ``total_ms`` and ``error=InterruptedException`` if
    Forge raises, or just empty stages if it returns cleanly).
    """
    with _hook_install_lock:
        try:
            from modules import processing as _proc
        except ImportError:
            return
        if getattr(_proc, _HOOK_INSTALL_FLAG, False):
            return
        setattr(_proc, _HOOK_INSTALL_FLAG, True)

        original_process = _proc.process_images_inner

        def wrapped_process_images_inner(p, *args, **kwargs):
            # Late check: if the current task was marked cancelled before
            # we got the lock, re-arm the interrupt flag that
            # ``state.begin()`` just cleared. The sampler exits at its
            # first interrupt-check (typically before step 0 of the
            # actual diffusion loop).
            try:
                from modules import progress, shared
                from . import cancelled_tasks
            except Exception:
                return original_process(p, *args, **kwargs)

            current = getattr(progress, "current_task", None)
            if current and cancelled_tasks.has(current):
                try:
                    shared.state.interrupted = True
                except Exception:
                    pass
                _log.info("late-abort: task %s preempted before sampling", current)
            return original_process(p, *args, **kwargs)

        _proc.process_images_inner = wrapped_process_images_inner
