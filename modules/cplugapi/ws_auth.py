"""WebSocket auth invariant shim for ``/cplugapi/v1/*``.

Invariant 4 in ``CLAUDE.md`` and the canonical Track 05 spec:
``/cplugapi/v1/*`` (HTTP and WebSocket upgrade) inherits the same
``--api-auth`` Basic auth as ``/sdapi/v1/*``. There are no WebSocket
endpoints today — ``T31`` (``/session/stream/{id_task}``) lands them
in Phase 2 — but the invariant is load-bearing for that phase, and
the doc claims it. W2 backs the claim with code so a future T31
implementation cannot silently regress: any WebSocket upgrade under
``/cplugapi/v1/*`` MUST present valid Basic credentials when
``--api-auth`` is configured, regardless of how the route was
attached.

Pure-ASGI middleware. Inserted into the cplugapi middleware stack
ahead of the HTTP-only layers so it sees the raw upgrade scope. No
op for HTTP traffic and for WebSocket paths outside the cplugapi
prefix — preserves invariant 1 (``/sdapi/v1/*`` byte-identity).

Forward-checked test (``tests/cplugapi/test_ws_auth.py``) registers
a stub WebSocket handler under ``/cplugapi/v1/_test/ws`` so the gate
exercises a real upgrade path. If T31 (or any other contributor)
later attaches a WS handler under the prefix without auth, the test
suite already covers the policy.

Rejection shape: HTTP 403 with the W3 problem+json envelope, sent
via the ``websocket.http.response.*`` ASGI events. Modern uvicorn
supports this. Clients (browsers, the Rust desktop client) see a
proper HTTP rejection on the upgrade rather than an abrupt close
code, which is the right UX for a credential-failure error.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from typing import Callable, Optional

from fastapi import FastAPI
from fastapi.security import HTTPBasicCredentials
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from . import capabilities
from .errors import CODES, PROBLEM_JSON

_log = logging.getLogger("cplugapi.ws_auth")
try:
    from backend.logging import setup_logger as _setup_logger

    _setup_logger(_log)
except ImportError:
    pass

_PREFIX = "/cplugapi/v1/"

_INSTALL_FLAG = "cplugapi_ws_auth_installed"
_install_lock = threading.Lock()


def _parse_basic_from_scope(scope: Scope) -> Optional[HTTPBasicCredentials]:
    """Decode the ``Authorization: Basic ...`` header from ASGI scope.

    Returns ``None`` for missing / malformed input — the caller turns
    that into a 403."""
    headers = scope.get("headers") or []
    auth_value: Optional[bytes] = None
    for name, value in headers:
        if name == b"authorization":
            auth_value = value
            break
    if not auth_value:
        return None
    try:
        scheme, _, encoded = auth_value.partition(b" ")
    except Exception:
        return None
    if scheme.lower() != b"basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip()).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    user, _, password = decoded.partition(":")
    return HTTPBasicCredentials(username=user, password=password)


async def _reject_403(send: Send, *, code: str, detail: str) -> None:
    """Reject a WS upgrade with HTTP 403 + problem+json body.

    Uses the ASGI ``websocket.http.response.*`` events; uvicorn-based
    servers translate these to a proper HTTP response on the upgrade
    socket. Clients see a 403 with ``application/problem+json`` body
    and never enter the WebSocket protocol.
    """
    body = json.dumps(
        {
            "type": "about:blank",
            "title": "Forbidden",
            "status": 403,
            "detail": detail,
            "code": code,
        }
    ).encode("utf-8")
    await send(
        {
            "type": "websocket.http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", PROBLEM_JSON.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "websocket.http.response.body", "body": body})


class CplugapiWsAuthShim:
    """Pure-ASGI middleware that enforces Basic auth on WS upgrades
    under ``/cplugapi/v1/*``.

    No-op for HTTP scopes, for non-cplugapi WS paths, and when no
    ``auth_dependency`` is configured (matching the HTTP surface
    posture: without ``--api-auth``, the surface is open).
    """

    def __init__(
        self, app: ASGIApp, auth_dependency: Optional[Callable] = None
    ) -> None:
        self.app = app
        self.auth_dependency = auth_dependency

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "websocket":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith(_PREFIX):
            await self.app(scope, receive, send)
            return
        if self.auth_dependency is None:
            # No --api-auth configured; cplugapi WS surface is open
            # (matches HTTP surface posture).
            await self.app(scope, receive, send)
            return

        creds = _parse_basic_from_scope(scope)
        if creds is None:
            _log.warning("cplugapi WS upgrade %s rejected: missing/malformed Basic auth", path)
            await _reject_403(
                send,
                code=CODES.AUTH_REQUIRED,
                detail="missing or malformed Basic credentials",
            )
            return

        try:
            self.auth_dependency(creds)
        except Exception as exc:
            _log.warning("cplugapi WS upgrade %s rejected: %s", path, exc)
            await _reject_403(
                send,
                code=CODES.AUTH_FAILED,
                detail="invalid credentials",
            )
            return

        # Auth OK — hand the upgrade off to the inner app.
        await self.app(scope, receive, send)


def install(app: FastAPI, auth_dependency: Optional[Callable] = None) -> None:
    """Attach the WS-auth shim. Idempotent + thread-safe.

    Inserts at the front of ``user_middleware`` so the shim runs
    outermost (sees raw upgrade scope before any other cplugapi
    middleware). HTTP traffic falls through immediately — pure-ASGI
    so no ``BaseHTTPMiddleware`` streaming-response footgun.
    """
    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(
            0, Middleware(CplugapiWsAuthShim, auth_dependency=auth_dependency)
        )
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Advertise that this build enforces WS auth."""
    capabilities.register("security/ws-auth-enforced")
