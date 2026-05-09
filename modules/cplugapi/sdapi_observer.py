"""Pure-ASGI request observer for ``/sdapi/v1/*``.

Why this exists separately from :mod:`access_log`:

The desktop ControlPlugin client fires generation requests at the
upstream ``/sdapi/v1/{txt2img,img2img}`` surface, not at our
``/cplugapi/v1/*`` prefix. Without observation on the upstream
prefix, "what is the client triggering?" is invisible — the artist
sees gens fire on their canvas with no record of the requests.

We can't put :class:`access_log.CplugapiAccessLogMiddleware` on
``/sdapi/v1/*`` because that middleware inherits from Starlette's
``BaseHTTPMiddleware``, which wraps every request through anyio task
groups + a memory-channel buffer. Gradio's long-poll endpoints under
``/sdapi/`` use ``StreamingResponse``, and the wrapper interacts
badly with streaming responses whose generators raise mid-flight
(encode/starlette#1438) — produces spurious ``RuntimeError: No
response returned`` and crashes the worker.

So this module uses pure ASGI: wraps the inner app's ``send``
callable to capture the response status code, times the call with
``perf_counter``, and emits one structured log line per request.
No body buffering (``receive`` is forwarded untouched), no anyio
task groups, no interaction with downstream streaming.

Path-scoped via prefix tuple — outside the prefixes the middleware
is a straight pass-through, no overhead, no log lines. Default
prefix is ``/sdapi/v1/`` only; ``/cplugapi/v1/`` is covered by the
existing :mod:`access_log` middleware so logging it here would
double-up.

**Read-only** — never mutates request or response bytes — preserves
the byte-identity invariant on ``/sdapi/v1/*`` (CLAUDE.md §1).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from fastapi import FastAPI

from . import capabilities

# Forge's logging helper — gives us the same rich-rendered ``name :: INFO``
# format as the rest of the boot output. Without it our log lines route
# through Python's default config which Forge has configured for stderr
# at WARNING level only — INFO-level messages get silently dropped.
try:
    from backend.logging import setup_logger as _setup_logger
except ImportError:
    _setup_logger = None  # OpenAPI export script / unit tests stub backend out

_log = logging.getLogger("cplugapi.sdapi")
if _setup_logger is not None:
    _setup_logger(_log)

# Env-var kill switch. Mirrors ``access_log.CPLUG_ACCESS_LOG``: read once
# at install, no hot-path env lookup. Disabling matters more here than
# for access_log because the desktop client polls /sdapi/v1/progress at
# ~4 Hz during a gen, which floods the console if the observer is
# active during normal operation. Default is ON to keep the diagnostic
# available out-of-the-box; the fork's webui-user.bat flips it off.
_ENV_DISABLE = "CPLUG_SDAPI_OBSERVER"


def _is_enabled() -> bool:
    raw = os.environ.get(_ENV_DISABLE)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# Default prefixes this observer logs. ``/cplugapi/v1/`` is omitted —
# :mod:`access_log` already produces a more detailed line for those.
DEFAULT_PREFIXES: tuple[str, ...] = ("/sdapi/v1/",)


def _content_length_from_scope(scope) -> int:
    """Return Content-Length as int, or -1 when absent / non-numeric.

    Header values in ASGI scopes are bytes; the iteration is cheap
    enough to do per request rather than building a CIMultiDict.
    """
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return -1
    return -1


class SdapiRequestObserver:
    """Pure-ASGI middleware that logs one line per matching request.

    The middleware wraps the inner app's ``send`` to capture the
    response status code at ``http.response.start``, times the call
    with ``perf_counter``, and emits a structured log line on
    completion (including the exception path so failures are visible).

    Prefer this shape over ``BaseHTTPMiddleware`` for any
    cross-cutting observation that needs to cover routes returning
    streaming responses — see module docstring.
    """

    def __init__(
        self,
        app,
        prefixes: tuple[str, ...] = DEFAULT_PREFIXES,
        enabled: bool = True,
    ) -> None:
        self.app = app
        self.prefixes = prefixes
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if not self.enabled:
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not any(path.startswith(p) for p in self.prefixes):
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        method = scope.get("method", "?")
        in_bytes = _content_length_from_scope(scope)

        # Mutable holder so the inner send wrapper can stash the
        # status code without needing nonlocal binding gymnastics.
        captured = {"status": 0}

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message.get("status", 0)
            await send(message)

        error: Optional[str] = None
        try:
            await self.app(scope, receive, wrapped_send)
        except BaseException as exc:
            # Re-raise but record the exception name so the log line
            # still emits with a meaningful tag instead of status=0.
            error = type(exc).__name__
            raise
        finally:
            dur_ms = (time.perf_counter() - start) * 1000.0
            extra = {
                "method": method,
                "path": path,
                "status": captured["status"],
                "dur_ms": round(dur_ms, 1),
                "in_bytes": in_bytes,
            }
            if error is not None:
                extra["error"] = error
            rendered = (
                f"{method} {path} status={captured['status']} "
                f"dur_ms={dur_ms:.1f} in={in_bytes}"
            )
            if error is not None:
                rendered += f" error={error}"
            _log.info(rendered, extra=extra)


_INSTALL_FLAG = "cplugapi_sdapi_observer_installed"
_install_lock = threading.Lock()


def install(app: FastAPI, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> None:
    """Attach the observer to ``app``. Idempotent + thread-safe.

    Always installs the middleware so its enabled-state can flip across
    test runs without re-mounting; runtime gating is via the ``enabled``
    flag captured at install time from :func:`_is_enabled`.

    Inserts at position 0 of ``user_middleware`` so it runs OUTERMOST
    in the chain — its ``dur_ms`` covers every other cplugapi
    middleware plus the handler. Caller is responsible for invoking
    ``app.build_middleware_stack()`` once registration is done; the
    cplugapi router does this for the whole middleware bundle.
    """
    from starlette.middleware import Middleware

    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(
            0,
            Middleware(
                SdapiRequestObserver, prefixes=prefixes, enabled=_is_enabled()
            ),
        )
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Advertise the sdapi-side request observer (only when enabled)."""
    if _is_enabled():
        capabilities.register("sdapi-request-log")
