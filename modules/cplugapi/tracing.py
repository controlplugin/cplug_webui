"""W3C Trace Context propagation for ``/cplugapi/v1/*`` (W11).

Implements header-echo only — no span emission. The ``opentelemetry``
SDK is **not** a fork dependency, so this module deliberately does not
import it (or any otel-* package) at module level. SDK-driven span
creation is reserved for a future capability
(``observability/trace-context-w3c-spans``) when otel-sdk lands.

The W3C ``traceparent`` header (https://www.w3.org/TR/trace-context/)
encodes:

    version "-" trace-id "-" parent-id "-" trace-flags

Where:

* ``version``   — 2 hex chars (this module emits ``00``, the only
  spec-defined version today)
* ``trace-id``  — 32 hex chars (16 bytes); all-zero is invalid
* ``parent-id`` — 16 hex chars (8 bytes); all-zero is invalid
* ``trace-flags`` — 2 hex chars (this module emits ``00`` for fresh
  traces; the ``sampled`` bit is meaningless without an SDK)

Behaviour on each ``/cplugapi/v1/*`` HTTP request:

1. Read incoming ``traceparent`` header.
2. If absent OR malformed (wrong shape, all-zero ids, etc.) — generate
   a fresh one. Forging a malformed traceparent shouldn't break a
   request; clients with broken instrumentation get a clean trace
   started for them server-side.
3. Stash the full string on ``request.state.traceparent`` and the
   bare 32-char ``trace_id`` on ``request.state.trace_id``. Downstream
   code (access_log, metrics) can pick them up as structured fields.
4. Echo the canonical ``traceparent`` value on the response.

Pure-ASGI (NOT ``BaseHTTPMiddleware``). Two reasons:

* ``BaseHTTPMiddleware`` buffers streaming responses (Starlette
  issue #1438). The fork's WS endpoints (T31, future) and SSE-style
  body streaming on ``/sdapi/v1/*`` long-polls must not be affected.
* WS upgrades go through scopes too; pure-ASGI middleware can
  pass-through WebSocket upgrades unchanged, where ``BaseHTTPMiddleware``
  asserts ``scope["type"] == "http"``.

Path-scoped to ``/cplugapi/v1/*`` so the byte-identity invariant for
``/sdapi/v1/*`` (CLAUDE.md hard invariant 1) is preserved — outside
the prefix this is a straight pass-through with no header touch.
"""

from __future__ import annotations

import re
import secrets
import threading
from typing import Optional

from fastapi import FastAPI, Request
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import capabilities

# Lower-case key on outbound headers (HTTP/2 + ASGI convention) and the
# canonical name returned to consumers.
HEADER_NAME = "traceparent"

_PREFIX = "/cplugapi/v1"

# version-traceid-parentid-flags: 2 + 32 + 16 + 2 hex chars and 3 dashes.
# Anchored; case-sensitive lower-case per spec ("hex chars" defined as
# 0-9 a-f). We do NOT accept upper-case A-F — the spec says lower-case.
_VALID_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_INVALID_TRACE_ID = "0" * 32
_INVALID_PARENT_ID = "0" * 16

# Stamped onto ``app.state`` so a second ``install`` call (test reuse,
# webui reload) is a no-op rather than double-registering the layer.
_INSTALL_FLAG = "cplugapi_tracing_installed"
_install_lock = threading.Lock()


def _generate_traceparent() -> str:
    """Return a fresh W3C traceparent: ``00-<trace-id>-<parent-id>-00``.

    Uses ``secrets`` rather than ``random`` so trace ids are unguessable
    — even though they're not security-sensitive on their own, leaking
    a predictable id stream lets an attacker correlate requests across
    a debug log dump. ``secrets.token_hex`` returns lower-case hex,
    matching the W3C spec.
    """
    trace_id = secrets.token_hex(16)  # 16 bytes -> 32 hex chars
    parent_id = secrets.token_hex(8)  # 8 bytes  -> 16 hex chars
    return f"00-{trace_id}-{parent_id}-00"


def _validate_traceparent(value: str) -> Optional[tuple[str, str, str, str]]:
    """Parse + validate a candidate traceparent string.

    Returns ``(version, trace_id, parent_id, flags)`` on success, or
    ``None`` if any of:

    * the overall shape doesn't match the spec regex,
    * the trace-id is all-zero,
    * the parent-id is all-zero.

    Per the W3C spec these all-zero forms MUST be treated as invalid,
    and per §3.2.2.5 vendors SHOULD restart the trace. We do exactly
    that — see :class:`CplugapiTracingMiddleware`.
    """
    if not _VALID_RE.match(value):
        return None
    parts = value.split("-")
    version, trace_id, parent_id, flags = parts
    if trace_id == _INVALID_TRACE_ID or parent_id == _INVALID_PARENT_ID:
        return None
    return version, trace_id, parent_id, flags


def get_traceparent(request: Request) -> Optional[str]:
    """Return the canonical traceparent stashed by the middleware.

    Handlers that want it should prefer this helper over re-reading the
    inbound header — by the time a handler runs the middleware has
    already replaced any malformed inbound value with a fresh one.
    """
    return getattr(request.state, "traceparent", None)


def get_trace_id(request: Request) -> Optional[str]:
    """Return the bare 32-char trace-id stashed by the middleware."""
    return getattr(request.state, "trace_id", None)


class CplugapiTracingMiddleware:
    """Pure-ASGI ``traceparent`` propagation for ``/cplugapi/v1/*``.

    Coexists with WebSocket scopes by passing them through untouched —
    the W3C spec is HTTP-shaped and the WS-upgrade scope already went
    through the inbound HTTP-style header parse, so a future T31 WS
    endpoint should attach trace context separately if needed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not scope.get("path", "").startswith(_PREFIX):
            await self.app(scope, receive, send)
            return

        # Find inbound traceparent (header names are lower-case in ASGI).
        incoming: Optional[str] = None
        for name, value in scope.get("headers") or []:
            if name == b"traceparent":
                try:
                    incoming = value.decode("ascii")
                except UnicodeDecodeError:
                    incoming = None
                break

        parsed = _validate_traceparent(incoming) if incoming else None
        if parsed is None:
            traceparent = _generate_traceparent()
            # Fresh trace-id is the second segment.
            trace_id = traceparent.split("-")[1]
        else:
            # incoming was non-None to reach this branch (parsed is only
            # not-None when validation succeeded on a non-empty string).
            traceparent = incoming  # type: ignore[assignment]
            trace_id = parsed[1]

        # Stash on ``scope["state"]`` (a dict; FastAPI initialises it for
        # us, but be defensive — direct ASGI tests may not). Reading via
        # ``request.state.traceparent`` works because Starlette's
        # ``Request.state`` is a thin wrapper over this dict.
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["traceparent"] = traceparent
        scope["state"]["trace_id"] = trace_id

        # Wrap ``send`` so the canonical traceparent is appended to the
        # outbound response headers. If the inner app already wrote one
        # (it shouldn't — this middleware is the canonical source) we
        # still append rather than replace; clients that see two
        # traceparent headers per HTTP/1.1 §3.2.2 take the first, which
        # is ours since we append last on the way out.
        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                # Strip any traceparent the inner app emitted so the
                # canonical value is the one observed on the wire.
                headers = [(k, v) for k, v in headers if k != b"traceparent"]
                headers.append((b"traceparent", traceparent.encode("ascii")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped_send)


def install(app: FastAPI) -> None:
    """Attach the middleware to ``app``. Idempotent + thread-safe.

    Inserts at the front of ``user_middleware`` so the layer runs early
    enough that downstream middleware (access_log, request_id) can read
    ``scope["state"]["trace_id"]``. Caller must invoke
    ``app.build_middleware_stack()`` once registration is done — same
    contract as the other ``install`` helpers in this package.
    """
    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(0, Middleware(CplugapiTracingMiddleware))
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Advertise W3C traceparent header-echo on ``/cplugapi/v1/*``."""
    capabilities.register("observability/trace-context-w3c")
