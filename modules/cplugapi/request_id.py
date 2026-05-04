"""``X-Request-Id`` middleware for ``/cplugapi/v1/*``.

Every fork request gets a stable request id — either echoing the
client-supplied ``X-Request-Id`` header (the Rust ControlPlugin client
mints ``req_<ulid>`` at the call-site) or, if absent, a server-generated
``req_<token>`` value. The id is exposed on the response via the same
header so the Rust client can correlate logs across the WebUI / native
boundary, and stashed at ``request.state.request_id`` so downstream
handlers can use it (e.g. structured logging).

**Path-scoped to ``/cplugapi/v1/*``** so the ``/sdapi/v1/*`` byte-identity
invariant (CLAUDE.md hard invariant 1) is preserved: outside the prefix
this middleware is a no-op and adds no header.
"""

from __future__ import annotations

import secrets
import threading
from typing import Optional

from fastapi import FastAPI, Request
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Inbound header may use any of these casings — Starlette normalises
# ``request.headers`` to lowercase but we keep the canonical Title-Case
# string for outbound emission.
HEADER_NAME = "X-Request-Id"

# Generated id format: ``req_<token>`` where token is 16 base64url chars
# (12 random bytes). 96 bits of entropy is comfortably collision-free
# across the desktop client's lifetime; the ``req_`` prefix lets log
# scrapers and humans recognise the format at a glance.
_GENERATED_PREFIX = "req_"
_TOKEN_BYTES = 12

_PREFIX = "/cplugapi/v1"

# Stamped onto ``app.state`` so a second ``install`` call (test reuse,
# webui reload) is a no-op rather than double-registering the middleware.
_INSTALL_FLAG = "cplugapi_request_id_installed"
_install_lock = threading.Lock()


def _generate_request_id() -> str:
    return _GENERATED_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def get_request_id(request: Request) -> Optional[str]:
    """Return the request id stashed on ``request.state``, or ``None``.

    Handlers that want the id should prefer this helper over re-reading
    the header — by the time a handler runs the middleware has already
    canonicalised the value (generated one if absent).
    """
    return getattr(request.state, "request_id", None)


class CplugapiRequestIdMiddleware(BaseHTTPMiddleware):
    """Read / generate / echo ``X-Request-Id`` on ``/cplugapi/v1/*``.

    Outside the prefix the call is a straight pass-through so the
    ``/sdapi/v1/*`` surface stays byte-identical with upstream.
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(_PREFIX):
            return await call_next(request)

        incoming = request.headers.get(HEADER_NAME)
        request_id = incoming if incoming else _generate_request_id()
        request.state.request_id = request_id

        response: Response = await call_next(request)
        response.headers[HEADER_NAME] = request_id
        return response


def install(app: FastAPI) -> None:
    """Attach the middleware to ``app``. Idempotent + thread-safe.

    Inserts directly into ``user_middleware`` so the install path works
    post-launch (cplugapi mounts after Gradio has started). Caller must
    invoke ``app.build_middleware_stack()`` once registration is done.
    """
    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(0, Middleware(CplugapiRequestIdMiddleware))
        setattr(app.state, _INSTALL_FLAG, True)
