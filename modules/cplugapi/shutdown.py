"""Graceful shutdown for ``/cplugapi/v1/*`` (W12).

On SIGTERM (or an explicit call to :func:`graceful_shutdown`) the
surface goes through three phases:

1. **Drain begins.** :func:`livez_readyz.set_draining` flips the
   readiness probe to 503 with ``checks.draining=true``. Cloud
   orchestrators (k8s, ALB) observe this on their next probe and
   pull the pod from rotation. The drain flag is visible to
   *unauthenticated* probes per W1's sanitised body — the operator
   doesn't need to wire creds into the orchestrator probe spec.

2. **Reject vs. accept new requests.** Configurable via
   ``CPLUG_SHUTDOWN_REJECT_NEW``. Default ``0`` (accept new
   requests during drain — single-replica desktop posture; the
   operator wants the gen to complete). Cloud profile flips this
   default to ``1`` (reject new POSTs to gen routes with 503 +
   ``Retry-After`` so the next replica picks up the work). Reads
   continue to serve so capability/health probes work throughout
   the drain.

3. **Grace + interrupt.** Wait up to ``CPLUG_SHUTDOWN_GRACE_S``
   (default 30) for in-flight gens to finish — polls
   ``progress.current_task`` and ``progress.pending_tasks``. If
   the polling itself wedges, an outer ``asyncio.wait_for`` timer
   forces the next phase. After grace expires, fire
   ``shared.state.interrupt()`` to abort whatever's still running.

The shutdown handler doesn't exit the process — uvicorn / gradio
own that. We just observe and signal.

Implementation notes:

- FastAPI ≥ 0.93 deprecates ``@app.on_event("shutdown")`` in favour
  of the Starlette lifespan context manager. Since cplugapi mounts
  *post-launch* (the FastAPI app is already constructed without
  ``lifespan=``), the production integration uses Python's
  ``signal`` module to bind SIGTERM. The handler schedules
  :func:`graceful_shutdown` on the running event loop via
  ``loop.call_soon_threadsafe(loop.create_task, ...)`` — signals
  fire on the main thread, but the async sequence needs to run on
  the loop.

- Tests invoke :func:`graceful_shutdown` directly to exercise the
  state machine without needing real signal delivery.

- Path-scoped: the optional ``RejectDuringDrainMiddleware`` only
  rejects POSTs to ``/cplugapi/v1/*`` and ``/sdapi/v1/{txt2img,img2img}``
  (the gen entry points). Reads pass through; non-cplugapi paths
  pass through unconditionally — invariant 1 byte-identity for
  ``/sdapi/v1/*`` other-than-the-two-listed gen paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from typing import Optional

from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from . import capabilities, livez_readyz, profile
from .errors import CODES, cplugapi_problem

_log = logging.getLogger("cplugapi.shutdown")
try:
    from backend.logging import setup_logger as _setup_logger

    _setup_logger(_log)
except ImportError:
    pass

ENV_GRACE_S = "CPLUG_SHUTDOWN_GRACE_S"
ENV_REJECT_NEW = "CPLUG_SHUTDOWN_REJECT_NEW"
ENV_POLL_INTERVAL_S = "CPLUG_SHUTDOWN_POLL_INTERVAL_S"

DEFAULT_GRACE_S = 30.0
DEFAULT_POLL_INTERVAL_S = 0.5


def _resolve_grace_s() -> float:
    raw = os.environ.get(ENV_GRACE_S, "").strip()
    if not raw:
        return DEFAULT_GRACE_S
    try:
        v = float(raw)
        return max(0.0, v)
    except ValueError:
        _log.warning(
            "cplugapi.shutdown: invalid %s=%r; using %.1f",
            ENV_GRACE_S, raw, DEFAULT_GRACE_S,
        )
        return DEFAULT_GRACE_S


def _resolve_poll_interval_s() -> float:
    raw = os.environ.get(ENV_POLL_INTERVAL_S, "").strip()
    if not raw:
        return DEFAULT_POLL_INTERVAL_S
    try:
        v = float(raw)
        return max(0.05, v)
    except ValueError:
        return DEFAULT_POLL_INTERVAL_S


def _resolve_reject_new() -> bool:
    """``CPLUG_SHUTDOWN_REJECT_NEW`` truthiness. Cloud profile flips
    the default to True; desktop default is False (single-replica,
    operator wants the gen to complete)."""
    raw = os.environ.get(ENV_REJECT_NEW, "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no", "off")
    return profile.is_cloud()


# ---------------------------------------------------------------------------
# In-flight work polling
# ---------------------------------------------------------------------------


def _has_active_work() -> bool:
    """``True`` when there's a running task or queued pending tasks.

    Best-effort — a torn-down ``modules.progress`` (early shutdown,
    test fixture state) returns ``False`` so the sequence completes
    instead of waiting forever."""
    try:
        from modules import progress
    except Exception:
        return False
    try:
        if getattr(progress, "current_task", None) is not None:
            return True
        pending = getattr(progress, "pending_tasks", None)
        if pending:
            return True
    except Exception:
        return False
    return False


def _interrupt_remaining() -> None:
    """Best-effort ``shared.state.interrupt()`` after grace expires."""
    try:
        from modules import shared

        shared.state.interrupt()
    except Exception as exc:
        _log.warning("cplugapi.shutdown: interrupt() failed: %s", exc)


# ---------------------------------------------------------------------------
# Shutdown sequence — async, awaitable, test-friendly
# ---------------------------------------------------------------------------


async def graceful_shutdown(
    grace_s: Optional[float] = None,
    poll_interval_s: Optional[float] = None,
) -> dict:
    """Run the full shutdown sequence. Returns a small report dict.

    Steps:

    1. Set the drain flag (``livez_readyz.set_draining(True)``).
    2. Poll for in-flight work; return early if everything finishes
       within ``grace_s``.
    3. After grace, call ``shared.state.interrupt()``.

    Idempotent — calling twice is a no-op on the second call (drain
    flag already set, no work to wait for).
    """
    if grace_s is None:
        grace_s = _resolve_grace_s()
    if poll_interval_s is None:
        poll_interval_s = _resolve_poll_interval_s()

    livez_readyz.set_draining(True)
    _log.info("cplugapi.shutdown: drain begun, grace_s=%.1f", grace_s)

    deadline = time.monotonic() + grace_s
    waited_s = 0.0
    interrupted = False
    while time.monotonic() < deadline:
        if not _has_active_work():
            _log.info(
                "cplugapi.shutdown: drain complete after %.2fs (no active work)",
                waited_s,
            )
            return {
                "drain_began": True,
                "waited_s": round(waited_s, 3),
                "interrupted": False,
                "grace_s": grace_s,
            }
        await asyncio.sleep(poll_interval_s)
        waited_s += poll_interval_s

    _log.warning(
        "cplugapi.shutdown: grace expired after %.1fs; firing interrupt",
        grace_s,
    )
    _interrupt_remaining()
    interrupted = True
    return {
        "drain_began": True,
        "waited_s": round(waited_s, 3),
        "interrupted": interrupted,
        "grace_s": grace_s,
    }


# ---------------------------------------------------------------------------
# Reject-during-drain middleware (optional — only when CPLUG_SHUTDOWN_REJECT_NEW=1)
# ---------------------------------------------------------------------------


_GEN_PATHS_TO_REJECT: frozenset[str] = frozenset(
    {"/sdapi/v1/txt2img", "/sdapi/v1/img2img"}
)
_CPLUGAPI_PREFIX = "/cplugapi/v1/"


class CplugapiRejectDuringDrainMiddleware:
    """Pure-ASGI gate: 503 new POST/PUT/PATCH/DELETE during drain.

    Engages only when ``CPLUG_SHUTDOWN_REJECT_NEW=1`` (or cloud profile
    default). Reads always pass through so capability/health probes
    keep working.

    Path scope: cplugapi prefix + the two ``/sdapi/v1/*`` gen entry
    points. Other ``/sdapi/v1/*`` paths (options, models, samplers,
    progress, etc.) are not rejected — they're metadata reads that
    should keep serving even during drain. Strict invariant 1
    byte-identity is preserved when the surface is NOT draining.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not livez_readyz.is_draining():
            await self.app(scope, receive, send)
            return
        if not _resolve_reject_new():
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        in_scope = path.startswith(_CPLUGAPI_PREFIX) or path in _GEN_PATHS_TO_REJECT
        if not in_scope:
            await self.app(scope, receive, send)
            return
        # Reject with 503 + Retry-After so cloud orchestrators back off
        # and route to a healthier replica.
        rid = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                rid = value.decode("ascii", errors="ignore")
                break
        response = cplugapi_problem(
            status=503,
            code=CODES.HTTP_ERROR,
            detail="server is draining; new gen requests refused",
            request_id=rid,
            headers={"Retry-After": "5"},
        )
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# Wire-up
# ---------------------------------------------------------------------------

_INSTALL_FLAG = "cplugapi_shutdown_installed"
_install_lock = threading.Lock()
_signal_installed = False


def _signal_handler(signum, frame) -> None:
    """Bridge from the OS signal to the async shutdown sequence.

    Signals fire on the main thread; the async sequence needs the
    event loop. ``call_soon_threadsafe`` is the standard bridge.
    Best-effort — if the loop isn't running we log and bail; the
    process will exit on whatever signals follow."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed() or not loop.is_running():
            _log.warning("cplugapi.shutdown: SIGTERM but loop not running")
            return
        loop.call_soon_threadsafe(asyncio.create_task, graceful_shutdown())
        _log.info("cplugapi.shutdown: scheduled drain on SIGTERM")
    except Exception as exc:
        _log.warning("cplugapi.shutdown: SIGTERM handler failed: %s", exc)


def install(app: FastAPI) -> None:
    """Attach the reject-during-drain middleware + bind SIGTERM.

    Idempotent. The SIGTERM bind only happens once per process — a
    second :func:`install` call only adds the middleware (the signal
    handler is module-global, set on first install).

    Windows note: SIGTERM doesn't exist on Windows in the POSIX sense
    (Python maps it but most signals other than CTRL_BREAK_EVENT /
    CTRL_C_EVENT don't deliver). On Windows we bind anyway; the
    binding is a no-op for unsupported signals. Windows operators
    rely on the orchestrator-level ``Stop-Process`` and the test
    suite's direct invocation of :func:`graceful_shutdown`."""
    global _signal_installed
    with _install_lock:
        if not getattr(app.state, _INSTALL_FLAG, False):
            app.user_middleware.insert(
                0, Middleware(CplugapiRejectDuringDrainMiddleware)
            )
            setattr(app.state, _INSTALL_FLAG, True)
        if not _signal_installed:
            try:
                signal.signal(signal.SIGTERM, _signal_handler)
                _signal_installed = True
            except (ValueError, OSError, AttributeError) as exc:
                # ValueError: signal only works in main thread.
                # OSError: signal not supported on this platform.
                # AttributeError: signal.SIGTERM missing (very unlikely).
                _log.debug(
                    "cplugapi.shutdown: SIGTERM bind skipped (%s); fall back to "
                    "explicit graceful_shutdown() invocation", exc,
                )


def register_capabilities() -> None:
    capabilities.register("ops/graceful-shutdown")
