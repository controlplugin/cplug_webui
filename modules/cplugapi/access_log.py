"""Per-request access log for ``/cplugapi/v1/*``.

Emits one structured log line per request with end-to-end server-side
timing — used to triage "where is the slow coming from" complaints
from the desktop client. The line includes:

- ``request_id`` — same value as the ``X-Request-Id`` header, so client
  and server logs can be joined.
- ``method`` / ``path`` / ``status`` — what was called and how it ended.
- ``dur_ms`` — wall-clock from the moment our middleware accepted the
  request to the moment we wrote the response. This is "us" — anything
  the client measures that's larger is network or client-side.
- ``in`` / ``out`` bytes — request and response Content-Length when
  declared. Streaming responses (none today) report ``out=-1``.
- ``replayed`` — set on idempotency-cache replays so cheap-second-call
  patterns don't look like a real handler executed.

**Outermost in the middleware chain by design.** The install order in
:func:`router._install_middlewares` puts this layer last (Starlette
runs most-recently-added first), so the timing it records spans every
other cplugapi middleware (security, request_id, idempotency) plus the
handler. That is exactly the number we want when diagnosing whose
clock is wrong.

**Path-scoped to ``/cplugapi/v1/*``** so ``/sdapi/v1/*`` byte-identity
(CLAUDE.md hard invariant 1) is preserved: outside the prefix this
middleware is a straight pass-through, no log lines, no overhead.

Output goes to the ``cplugapi.access`` logger so operators can route
or silence it independently of the rest of the cplugapi log stream.
The level is INFO by default; set ``CPLUG_ACCESS_LOG=0`` to disable
entirely (the middleware still installs but skips emission, keeping
the perf cost to one branch per request).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from . import capabilities

_PREFIX = "/cplugapi/v1"

# Dedicated logger so users can route access output independently from
# warnings/errors emitted by the rest of the cplugapi modules. Routed
# through Forge's setup_logger so the lines appear on console with the
# same ``name :: INFO`` formatting as the boot output — Python's
# default logging config (which Forge inherits implicitly) drops
# INFO-level messages on stderr otherwise.
_log = logging.getLogger("cplugapi.access")
try:
    from backend.logging import setup_logger as _setup_logger
    _setup_logger(_log)
except ImportError:
    pass  # OpenAPI export / tests stub backend out

# Env-var kill switch. Read once at install time so toggling it requires
# a restart — the middleware is meant to be cheap (sub-microsecond when
# disabled), not runtime-pluggable.
_ENV_DISABLE = "CPLUG_ACCESS_LOG"


def _is_enabled() -> bool:
    """``CPLUG_ACCESS_LOG=0`` (or ``false``/``no``/``off``) disables emission.

    Anything else, including unset, leaves emission on.
    """
    raw = os.environ.get(_ENV_DISABLE)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _content_length(headers) -> int:
    """Return Content-Length as int, or ``-1`` when absent / non-numeric.

    ``-1`` is used because ``0`` is a legitimate value (empty body).
    """
    raw = headers.get("content-length")
    if raw is None:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


class CplugapiAccessLogMiddleware(BaseHTTPMiddleware):
    """One log line per ``/cplugapi/v1/*`` request, with end-to-end timing.

    Outside the prefix the call is a straight pass-through. Inside, the
    middleware:

    1. Records ``perf_counter()`` on entry.
    2. Defers to the rest of the chain.
    3. On the way out (including exceptions), emits a single structured
       line. Exceptions are re-raised after logging so error semantics
       are unchanged.
    """

    def __init__(self, app, enabled: bool = True) -> None:
        super().__init__(app)
        self._enabled = enabled

    async def __call__(self, scope, receive, send):
        # Bypass ``BaseHTTPMiddleware``'s anyio-task-group wrapper on
        # paths we don't measure. The wrapper buffers responses through
        # a channel, which deadlocks / mis-attributes errors when a
        # downstream endpoint returns a ``StreamingResponse`` whose
        # generator raises (Gradio's long-poll endpoints do this on
        # client disconnect — Starlette issue 1438). Pure passthrough
        # for non-cplugapi paths preserves byte-identity AND sidesteps
        # the bug. Only paths under ``/cplugapi/v1/`` go through the
        # wrapping machinery, and those endpoints don't stream.
        if scope["type"] != "http" or not scope.get("path", "").startswith(_PREFIX):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # By the time dispatch runs we've already filtered to cplugapi
        # paths via __call__, so the prefix check here would be
        # redundant. Kept as a defensive guard for direct unit tests
        # that bypass __call__.
        if not request.url.path.startswith(_PREFIX):
            return await call_next(request)

        if not self._enabled:
            return await call_next(request)

        start = time.perf_counter()
        in_bytes = _content_length(request.headers)
        method = request.method
        path = request.url.path

        status: int = 0
        out_bytes: int = -1
        replayed: bool = False
        error_name: Optional[str] = None
        try:
            response = await call_next(request)
        except Exception as exc:
            # Log the failed-to-respond case with the same shape so a
            # log scraper sees consistent fields. Status 500 is the
            # canonical "we crashed before producing a response" code
            # Starlette will eventually emit anyway.
            error_name = type(exc).__name__
            status = 500
            self._emit(method, path, status, in_bytes, out_bytes, replayed,
                       (time.perf_counter() - start) * 1000.0,
                       request_id=getattr(request.state, "request_id", None),
                       error_name=error_name,
                       traceparent=getattr(request.state, "traceparent", None),
                       trace_id=getattr(request.state, "trace_id", None))
            raise

        status = response.status_code
        out_bytes = _content_length(response.headers)
        replayed = response.headers.get("Idempotency-Replayed", "").lower() == "true"

        # Pull request_id off state — populated by the request_id middleware.
        # When access_log runs OUTSIDE request_id (as it does in the
        # default install order), state is already stamped by the time
        # call_next returns.
        request_id = getattr(request.state, "request_id", None)
        # W11 — pull traceparent / trace_id off state so JSON-mode log
        # scrapers can correlate cplugapi access lines to distributed
        # traces. Both fields are populated by the W11 tracing
        # middleware (sibling to request_id, runs OUTSIDE access_log
        # in the canonical stack but INSIDE access_log's call_next at
        # the time we read state).
        traceparent = getattr(request.state, "traceparent", None)
        trace_id = getattr(request.state, "trace_id", None)

        self._emit(method, path, status, in_bytes, out_bytes, replayed,
                   (time.perf_counter() - start) * 1000.0,
                   request_id=request_id,
                   traceparent=traceparent,
                   trace_id=trace_id)
        return response

    @staticmethod
    def _emit(
        method: str,
        path: str,
        status: int,
        in_bytes: int,
        out_bytes: int,
        replayed: bool,
        dur_ms: float,
        request_id: Optional[str],
        error_name: Optional[str] = None,
        traceparent: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """Format + emit the access line.

        Format is grep-friendly key=value, all on one line. Order is
        intentionally fixed so a regex / column splitter is stable.
        Switch to JSON by replacing this method's body — call sites
        do not change.
        """
        # ``extra`` is also populated so callers using a structured
        # JSON formatter (W9) get the same fields without parsing the
        # rendered message.
        extra = {
            "request_id": request_id,
            "method": method,
            "path": path,
            "status": status,
            "in_bytes": in_bytes,
            "out_bytes": out_bytes,
            "replayed": replayed,
            "dur_ms": round(dur_ms, 3),
        }
        if error_name is not None:
            extra["error"] = error_name
        if traceparent is not None:
            extra["traceparent"] = traceparent
        if trace_id is not None:
            extra["trace_id"] = trace_id

        rendered = (
            f"{method} {path} status={status} "
            f"dur_ms={dur_ms:.3f} "
            f"in={in_bytes} out={out_bytes} "
            f"req_id={request_id or '-'}"
        )
        if replayed:
            rendered += " replayed=1"
        if error_name is not None:
            rendered += f" error={error_name}"
        if trace_id is not None:
            rendered += f" trace_id={trace_id}"

        _log.info(rendered, extra=extra)


_INSTALL_FLAG = "cplugapi_access_log_installed"
_install_lock = threading.Lock()


def install(app: FastAPI) -> None:
    """Attach the access-log middleware to ``app``. Idempotent + thread-safe.

    Caller (router) is responsible for ``app.build_middleware_stack()``
    after all cplugapi middlewares are registered. Install ORDER in
    :func:`router._install_middlewares` matters — this layer wants to
    sit outermost so it sees the same wall clock the network sees.
    """
    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(
            0, Middleware(CplugapiAccessLogMiddleware, enabled=_is_enabled())
        )
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Advertise that this build emits per-request access logs.

    W15 — dual-emits ``observability/request-log`` (new) and
    ``request-log`` (legacy). The legacy string is marked deprecated;
    it'll be dropped in the next minor after the Rust client confirms
    migration."""
    capabilities.register_with_legacy(
        new_name="observability/request-log",
        legacy_name="request-log",
    )
