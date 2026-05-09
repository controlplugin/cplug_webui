"""ASGI middleware that hardens ``/cplugapi/v1/*`` against three threats.

Threat model
------------

The cplugapi surface is bound to a loopback interface and inherits Basic
auth from ``--api-auth``, but the live-sketching client is a *desktop*
peer talking to a *web server* — that combination opens three browser-
adjacent attack surfaces that ``/sdapi/v1/*`` historically ignores.

1. **CSRF / cross-origin abuse.** Any page the artist visits in a browser
   on the same machine can ``fetch('http://127.0.0.1:7860/cplugapi/v1/…')``
   and ride along on Basic-auth credentials cached by the browser. We
   reject requests whose ``Origin`` or ``Sec-Fetch-Site`` headers say
   the call came from a hostile context.

2. **DNS rebinding.** A remote attacker registers ``evil.example``,
   resolves it briefly to their own IP, ships JS, then re-binds the
   record to ``127.0.0.1``. The browser still considers the origin
   ``evil.example`` (so SOP "protects" the attacker) but TCP now hits
   the local backend. The mitigation is a strict ``Host`` allow-list:
   anything that is not exactly one of the loopback names is rejected
   regardless of where the TCP packet came from.

3. **Body-size DoS / zip-bomb.** The cplugapi endpoints are tiny (a
   task ID, a preset name, a JSON struct of options). A multi-MB POST
   has no legitimate purpose here and only exists to exhaust buffers
   or compression units further down the stack. Cap by
   ``Content-Length``.

The middleware applies ONLY to paths under ``/cplugapi/v1/``; any other
path (most importantly ``/sdapi/v1/*``) passes through untouched, which
preserves the byte-identical-to-upstream invariant called out in
``CLAUDE.md`` §1.

Wiring (handled in ``router.py`` follow-up):

.. code-block:: python

    from . import security_middleware
    security_middleware.install(app)
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Awaitable, Callable, Iterable

from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

_log = logging.getLogger(__name__)

# Path prefix this middleware guards. Anything outside is a no-op so
# ``/sdapi/v1/*`` byte-identity with upstream is preserved.
PROTECTED_PREFIX = "/cplugapi/v1/"

# Default Origin allow-list. Empty/missing Origin (native client) and the
# literal string ``null`` (file:// pages, sandboxed frames) are accepted
# specially in code; this list is only consulted for non-empty,
# non-``null`` Origin values that fail the loopback regex.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = ()

# Loopback Origin pattern. ``http://`` only — HTTPS to a loopback bind is
# unusual enough that the few legitimate users can opt in via
# ``CPLUG_ALLOWED_ORIGINS``.
_LOOPBACK_ORIGIN_RE = re.compile(
    r"^http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$"
)

# Default Host allow-list. Includes both bare-host (HTTP/1.0 or older
# clients) and host:port forms for the common dev ports. ``[::1]`` is
# the bracketed IPv6 loopback per RFC 3986.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "127.0.0.1",
    "localhost",
    "[::1]",
)

# Pattern for the host:port form of the loopback names. Built once so
# the per-request check stays O(1).
_LOOPBACK_HOST_RE = re.compile(
    r"^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$"
)

# Sec-Fetch-Site values that are unambiguously safe. ``cross-site`` and
# ``same-site`` are the rejected cases per the threat model: no legitimate
# page on a different origin should be initiating cplugapi calls.
_SAFE_FETCH_SITES: frozenset[str] = frozenset({"none", "same-origin"})

# Body-size cap. 32 MiB is generous for the largest legitimate cplugapi
# payload (a base64 mask) while small enough to refuse zip-bomb shaped
# input. Override per deployment via ``CPLUG_MAX_BODY_BYTES``.
DEFAULT_MAX_BODY_BYTES: int = 32 * 1024 * 1024

# Env-var names. Documented here so tests can monkeypatch the same
# strings the install() helper reads.
ENV_ALLOWED_ORIGINS = "CPLUG_ALLOWED_ORIGINS"
ENV_ALLOWED_HOSTS = "CPLUG_ALLOWED_HOSTS"
ENV_MAX_BODY_BYTES = "CPLUG_MAX_BODY_BYTES"


def _parse_csv_env(name: str) -> tuple[str, ...]:
    """Read a comma-separated env var into a tuple. Empty entries dropped."""
    raw = os.environ.get(name, "")
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_int_env(name: str, default: int) -> int:
    """Read an int env var. Falls back to ``default`` on missing/garbage."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        _log.warning(
            "cplugapi: ignoring invalid %s=%r (expected integer); using %d",
            name, raw, default,
        )
        return default


def _reject(detail: str, status_code: int) -> JSONResponse:
    """Build the canonical rejection response. Body shape mirrors FastAPI's
    ``HTTPException``-default JSON so clients can use one decoder path."""
    return JSONResponse({"detail": detail}, status_code=status_code)


class CplugapiSecurityMiddleware(BaseHTTPMiddleware):
    """Path-scoped CSRF / DNS-rebinding / body-size guard.

    Configuration is captured once at construction time. Re-reading env
    vars per request would race with subprocess-level env mutation and
    add measurable overhead for an endpoint that is already
    sub-millisecond on the happy path.

    Thread safety: the middleware is stateless after ``__init__``; the
    only mutable state is the immutable tuples held on ``self``.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_origins: Iterable[str] | None = None,
        allowed_hosts: Iterable[str] | None = None,
        max_body_bytes: int | None = None,
    ) -> None:
        super().__init__(app)
        self._allowed_origins: frozenset[str] = frozenset(
            allowed_origins
            if allowed_origins is not None
            else (*DEFAULT_ALLOWED_ORIGINS, *_parse_csv_env(ENV_ALLOWED_ORIGINS))
        )
        self._allowed_hosts: frozenset[str] = frozenset(
            allowed_hosts
            if allowed_hosts is not None
            else (*DEFAULT_ALLOWED_HOSTS, *_parse_csv_env(ENV_ALLOWED_HOSTS))
        )
        self._max_body_bytes: int = (
            max_body_bytes
            if max_body_bytes is not None
            else _parse_int_env(ENV_MAX_BODY_BYTES, DEFAULT_MAX_BODY_BYTES)
        )

    async def __call__(self, scope, receive, send):
        # Bypass ``BaseHTTPMiddleware``'s anyio task-group wrapper on
        # paths outside the protected prefix. The wrapper buffers
        # responses through a channel that mis-attributes errors when
        # an inner ``StreamingResponse`` raises mid-stream (Gradio's
        # long-poll endpoints, Starlette issue 1438). Pure passthrough
        # for non-cplugapi paths preserves the upstream response shape
        # exactly AND sidesteps the bug — only requests we genuinely
        # need to inspect go through the wrapping machinery.
        if scope["type"] != "http" or not scope.get("path", "").startswith(PROTECTED_PREFIX):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Path scope — anything outside ``/cplugapi/v1/`` is upstream's
        # surface and must pass through unchanged.
        if not request.url.path.startswith(PROTECTED_PREFIX):
            return await call_next(request)

        rejection = self._check_origin(request)
        if rejection is not None:
            return rejection

        rejection = self._check_fetch_site(request)
        if rejection is not None:
            return rejection

        rejection = self._check_host(request)
        if rejection is not None:
            return rejection

        rejection = self._check_body_size(request)
        if rejection is not None:
            return rejection

        return await call_next(request)

    # --- individual checks -------------------------------------------------

    def _check_origin(self, request: Request) -> Response | None:
        """Reject Origins that are neither absent, ``null``, loopback, nor
        in the configured allow-list."""
        origin = request.headers.get("origin")
        # No header — typical for native clients (Tauri/Electron with
        # ``Origin`` stripped, curl, server-to-server). Fall through.
        if origin is None or origin == "":
            return None
        # ``null`` is what browsers send for file:// pages and sandboxed
        # iframes. Trusted enough for the desktop-companion threat model;
        # cross-origin pages do not get to forge this string because the
        # browser writes it.
        if origin == "null":
            return None
        if _LOOPBACK_ORIGIN_RE.match(origin):
            return None
        if origin in self._allowed_origins:
            return None
        _log.warning("cplugapi: rejecting Origin %r", origin)
        return _reject(f"origin not allowed: {origin}", 403)

    def _check_fetch_site(self, request: Request) -> Response | None:
        """Reject ``Sec-Fetch-Site: cross-site`` / ``same-site``.

        ``none`` is the value the browser sets for navigation initiated
        outside any document (address-bar entry, bookmark) and for native
        clients that surface the header. ``same-origin`` is fine. Header
        absence is treated as legacy/native and allowed."""
        sfs = request.headers.get("sec-fetch-site")
        if sfs is None:
            return None
        if sfs in _SAFE_FETCH_SITES:
            return None
        _log.warning("cplugapi: rejecting Sec-Fetch-Site %r", sfs)
        return _reject(f"sec-fetch-site not allowed: {sfs}", 403)

    def _check_host(self, request: Request) -> Response | None:
        """DNS-rebinding mitigation: Host must be exact-match loopback."""
        host = request.headers.get("host")
        # Absent Host can happen on HTTP/1.0; uvicorn synthesizes one in
        # practice but we tolerate absence rather than reject blindly.
        if host is None or host == "":
            return None
        # Bare names (``127.0.0.1``) and host:port (``127.0.0.1:7860``)
        # variations both hit the regex. Anything outside it falls
        # through to the configured allow-list, which is exact-match —
        # crucial for rebinding defence so ``127.0.0.1.evil.example``
        # does not slip through a substring check.
        if _LOOPBACK_HOST_RE.match(host):
            return None
        if host in self._allowed_hosts:
            return None
        _log.warning("cplugapi: rejecting Host %r", host)
        return _reject(f"host not allowed: {host}", 403)

    def _check_body_size(self, request: Request) -> Response | None:
        """Reject oversized POST/PUT/PATCH bodies by ``Content-Length``.

        We do not buffer when the header is absent — the cplugapi
        endpoints all use small JSON or path params, so streaming-without-
        Content-Length is rare enough that downstream's own limits handle
        it without us paying for a per-request body buffer."""
        if request.method not in ("POST", "PUT", "PATCH"):
            return None
        cl = request.headers.get("content-length")
        if cl is None:
            return None
        try:
            size = int(cl)
        except ValueError:
            return _reject("invalid content-length", 400)
        if size > self._max_body_bytes:
            _log.warning(
                "cplugapi: rejecting Content-Length %d > %d",
                size, self._max_body_bytes,
            )
            return _reject(
                f"request body too large: {size} > {self._max_body_bytes}",
                413,
            )
        return None


# --- install / capabilities --------------------------------------------------

# A single FastAPI app should not get the middleware stacked twice — it
# would just add latency and double-log rejections. Track install state
# on ``app.state`` (same convention router.py uses for its own mount
# flag) and serialize on a module-level lock so concurrent test setups
# don't race.
_INSTALL_FLAG = "cplugapi_security_installed"
_install_lock = threading.Lock()


def install(app: FastAPI) -> None:
    """Add :class:`CplugapiSecurityMiddleware` to ``app``. Idempotent.

    Safe to call from a test fixture and from production wire-up; a
    second call on the same ``app`` is a no-op. Concurrent callers
    serialize on a module-level lock so route registration cannot race.

    Inserts into ``user_middleware`` directly rather than calling
    ``app.add_middleware`` — the cplugapi mount runs after the Gradio
    app has started and ``add_middleware`` rejects post-launch calls.
    Caller must invoke ``app.build_middleware_stack()`` after all
    middlewares are registered.
    """
    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(0, Middleware(CplugapiSecurityMiddleware))
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Advertise the protections this middleware provides.

    Called from ``router.setup_cplugapi`` (follow-up wire-up step) so
    ``/cplugapi/v1/health.capabilities[]`` lists the slash-only strings
    ``security/origin-checks``, ``security/host-checks``, and
    ``security/body-size-cap``."""
    from . import capabilities

    capabilities.register("security/origin-checks")
    capabilities.register("security/host-checks")
    capabilities.register("security/body-size-cap")
