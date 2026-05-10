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
from starlette.responses import Response
from starlette.types import ASGIApp

from . import profile
from .errors import CODES, cplugapi_problem

_log = logging.getLogger(__name__)

# Wildcard token recognised in Origin / Host allow-lists. When present,
# the corresponding check returns "allow" for any non-empty value. Used
# by the ``cloud`` deployment profile to disable defenses that the
# ingress already enforces (TLS, vhost routing, WAF). Operators can
# also opt in explicitly with ``CPLUG_ALLOWED_HOSTS=*``.
WILDCARD = "*"

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

# Per-route body-size caps (W7). The global 32 MiB default is right for
# the *largest* legitimate cplugapi payload (a base64 mask delivered to
# canvas/strokes) but wildly oversized for endpoints that accept only a
# preset name or a task id — those routes are trivially DoS-able with
# 32 MiB of JSON garbage that --api-auth doesn't stop. Each entry caps
# a (METHOD, prefix) pair; the matcher is "longest-prefix terminated by
# ``/`` or end-of-string" so a sibling path like ``/forge/preset-bulk``
# does NOT inherit the ``/forge/preset/`` cap. Operators override via
# ``CPLUG_ROUTE_BODY_LIMITS``.
ROUTE_LIMITS: dict[tuple[str, str], int] = {
    ("POST", "/cplugapi/v1/forge/preset/"): 4 * 1024,
    ("POST", "/cplugapi/v1/session/cancel/"): 4 * 1024,
    ("POST", "/cplugapi/v1/session/preempt"): 4 * 1024,
}

# Env-var names. Documented here so tests can monkeypatch the same
# strings the install() helper reads.
ENV_ALLOWED_ORIGINS = "CPLUG_ALLOWED_ORIGINS"
ENV_ALLOWED_HOSTS = "CPLUG_ALLOWED_HOSTS"
ENV_MAX_BODY_BYTES = "CPLUG_MAX_BODY_BYTES"
ENV_ROUTE_BODY_LIMITS = "CPLUG_ROUTE_BODY_LIMITS"


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


def _parse_route_limits_env(name: str) -> dict[tuple[str, str], int]:
    """Parse ``CPLUG_ROUTE_BODY_LIMITS`` into the same shape as
    ``ROUTE_LIMITS``.

    Format: comma-separated ``METHOD:path:bytes`` triples, e.g.
    ``POST:/cplugapi/v1/forge/preset/:4096``. Empty/unset returns ``{}``,
    in which case the caller falls back to the built-in ``ROUTE_LIMITS``
    defaults rather than running with an empty table — that distinction
    matters because an explicit override of one route should not silently
    drop the protection on the others.

    Malformed entries are logged and skipped so a typo on one rule does
    not mask the others. Path is stored verbatim (the matcher applies
    the ``/`` / EOS termination rule at lookup time, not parse time).
    """
    raw = os.environ.get(name, "")
    if not raw:
        return {}
    out: dict[tuple[str, str], int] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        # Split from the right twice: method, then bytes. Path can
        # legitimately contain colons (e.g. ``/foo/bar:baz``) so a
        # naive ``split(":", 2)`` from the left would misparse.
        head, _, byte_str = entry.rpartition(":")
        method, _, path = head.partition(":")
        method = method.strip().upper()
        path = path.strip()
        byte_str = byte_str.strip()
        if not method or not path or not byte_str:
            _log.warning(
                "cplugapi: ignoring malformed %s entry %r "
                "(expected METHOD:path:bytes)",
                name, entry,
            )
            continue
        try:
            size = int(byte_str)
        except ValueError:
            _log.warning(
                "cplugapi: ignoring %s entry %r (bytes not an integer)",
                name, entry,
            )
            continue
        if size < 0:
            _log.warning(
                "cplugapi: ignoring %s entry %r (negative byte count)",
                name, entry,
            )
            continue
        out[(method, path)] = size
    return out


def _match_route_limit(
    method: str, path: str, table: dict[tuple[str, str], int]
) -> int | None:
    """Return the per-route cap for ``(method, path)`` if any rule
    matches, else ``None``.

    Match predicate: a rule prefix matches when ``path`` either equals
    the prefix or extends it with a ``/``-terminated segment. This is
    why ``/cplugapi/v1/forge/preset/sketch`` matches the
    ``/cplugapi/v1/forge/preset/`` rule (the prefix already ends in
    ``/``), but ``/cplugapi/v1/forge/preset-bulk`` does NOT — the
    character after the prefix boundary is ``-``, not ``/`` or EOS.

    When multiple rules match (a literal-prefix table can't *really*
    have overlapping rules under this predicate, but operators may
    create them via env var), we pick the *longest* prefix. A rule
    matching ``/a/b/c`` always wins over a rule matching ``/a/``."""
    best_len = -1
    best_size: int | None = None
    for (m, prefix), size in table.items():
        if m != method:
            continue
        if not path.startswith(prefix):
            continue
        # Prefix matches verbatim. Now check the boundary: either the
        # prefix already ends in '/' (so any continuation is a fresh
        # path segment), or the path stops exactly at the prefix end
        # (EOS), or the next character is '/'.
        if prefix.endswith("/"):
            ok = True
        elif len(path) == len(prefix):
            ok = True
        elif path[len(prefix)] == "/":
            ok = True
        else:
            ok = False
        if not ok:
            continue
        if len(prefix) > best_len:
            best_len = len(prefix)
            best_size = size
    return best_size


def _reject(detail: str, status_code: int, code: str, request: Request) -> Response:
    """Build the canonical rejection response — RFC 9457 problem+json
    envelope. ``code`` is a stable machine-switchable identifier from
    :class:`errors.CODES`.

    Note on request_id sourcing: this middleware sits OUTSIDE
    ``request_id`` in the canonical install order (per
    ``plan/cplugapi-world-class.md`` §3.0), so ``request.state.request_id``
    is unset when we reject. We fall back to the inbound
    ``X-Request-Id`` header so a client-supplied id still surfaces on
    the problem envelope; if the client didn't send one, we emit no
    ``request_id`` field rather than an unstamped placeholder."""
    rid = (
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-Id")
    )
    return cplugapi_problem(
        status=status_code,
        code=code,
        detail=detail,
        request_id=rid,
    )


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
        route_body_limits: dict[tuple[str, str], int] | None = None,
    ) -> None:
        super().__init__(app)
        # Profile-aware defaults (W5). Explicit constructor args win;
        # explicit env vars win next; profile defaults apply last.
        cloud = profile.is_cloud()
        if allowed_origins is not None:
            self._allowed_origins: frozenset[str] = frozenset(allowed_origins)
        else:
            env_origins = _parse_csv_env(ENV_ALLOWED_ORIGINS)
            if env_origins:
                self._allowed_origins = frozenset(env_origins)
            elif cloud:
                # Cloud ingress already filters cross-origin pages; the
                # Sec-Fetch-Site check still rejects ``cross-site``.
                self._allowed_origins = frozenset({WILDCARD})
            else:
                self._allowed_origins = frozenset(DEFAULT_ALLOWED_ORIGINS)
        if allowed_hosts is not None:
            self._allowed_hosts: frozenset[str] = frozenset(allowed_hosts)
        else:
            env_hosts = _parse_csv_env(ENV_ALLOWED_HOSTS)
            if env_hosts:
                self._allowed_hosts = frozenset(env_hosts)
            elif cloud:
                # In cloud profile the ingress controls vhost routing;
                # rebind defence at our layer is redundant.
                self._allowed_hosts = frozenset({WILDCARD})
            else:
                self._allowed_hosts = frozenset(DEFAULT_ALLOWED_HOSTS)
        self._max_body_bytes: int = (
            max_body_bytes
            if max_body_bytes is not None
            else _parse_int_env(ENV_MAX_BODY_BYTES, DEFAULT_MAX_BODY_BYTES)
        )
        # Per-route caps (W7). Constructor arg wins; explicit env wins
        # next; the built-in ROUTE_LIMITS table is the floor. Empty env
        # falls through to defaults intentionally — operators removing
        # all route caps would have to set the env var to a single
        # nonsense entry that we then drop, which is clearly wrong; if
        # they need to neuter the table they set per-route caps to a
        # value larger than the global cap.
        if route_body_limits is not None:
            self._route_body_limits: dict[tuple[str, str], int] = dict(
                route_body_limits
            )
        else:
            env_limits = _parse_route_limits_env(ENV_ROUTE_BODY_LIMITS)
            if env_limits:
                self._route_body_limits = env_limits
            else:
                self._route_body_limits = dict(ROUTE_LIMITS)

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
        # Wildcard ``*`` accepts any non-empty Origin. Set by the
        # ``cloud`` deployment profile or by an explicit
        # ``CPLUG_ALLOWED_ORIGINS=*``. Sec-Fetch-Site still rejects
        # ``cross-site`` even with the wildcard set.
        if WILDCARD in self._allowed_origins:
            return None
        if origin in self._allowed_origins:
            return None
        _log.warning("cplugapi: rejecting Origin %r", origin)
        return _reject(
            f"origin not allowed: {origin}",
            403,
            CODES.ORIGIN_NOT_ALLOWED,
            request,
        )

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
        return _reject(
            f"sec-fetch-site not allowed: {sfs}",
            403,
            CODES.SEC_FETCH_SITE_NOT_ALLOWED,
            request,
        )

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
        # Wildcard accepts any non-empty Host. The DNS-rebind threat
        # is irrelevant in cloud profile where the ingress enforces
        # vhost routing before traffic reaches us.
        if WILDCARD in self._allowed_hosts:
            return None
        if host in self._allowed_hosts:
            return None
        _log.warning("cplugapi: rejecting Host %r", host)
        return _reject(
            f"host not allowed: {host}",
            403,
            CODES.HOST_NOT_ALLOWED,
            request,
        )

    def _check_body_size(self, request: Request) -> Response | None:
        """Reject oversized POST/PUT/PATCH bodies by ``Content-Length``.

        We do not buffer when the header is absent — the cplugapi
        endpoints all use small JSON or path params, so streaming-without-
        Content-Length is rare enough that downstream's own limits handle
        it without us paying for a per-request body buffer.

        Per-route caps (W7) take precedence when the request matches a
        rule in the route-limits table. The longest matching prefix
        wins; non-matching routes fall through to the global cap."""
        if request.method not in ("POST", "PUT", "PATCH"):
            return None
        cl = request.headers.get("content-length")
        if cl is None:
            return None
        try:
            size = int(cl)
        except ValueError:
            return _reject(
                "invalid content-length",
                400,
                CODES.INVALID_CONTENT_LENGTH,
                request,
            )
        # Per-route cap takes precedence over the global cap. The
        # detail string distinguishes the two so operators can tell
        # from a 413 log line which limit was hit.
        route_cap = _match_route_limit(
            request.method, request.url.path, self._route_body_limits
        )
        if route_cap is not None and size > route_cap:
            _log.warning(
                "cplugapi: rejecting Content-Length %d > %d "
                "(route-specific cap for %s %s)",
                size, route_cap, request.method, request.url.path,
            )
            return _reject(
                f"request body too large: {size} > {route_cap} "
                f"(route-specific limit: {route_cap} bytes)",
                413,
                CODES.BODY_TOO_LARGE,
                request,
            )
        if size > self._max_body_bytes:
            _log.warning(
                "cplugapi: rejecting Content-Length %d > %d",
                size, self._max_body_bytes,
            )
            return _reject(
                f"request body too large: {size} > {self._max_body_bytes}",
                413,
                CODES.BODY_TOO_LARGE,
                request,
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
    ``security/origin-checks``, ``security/host-checks``,
    ``security/body-size-cap``, and ``security/per-route-body-limits``."""
    from . import capabilities

    capabilities.register("security/origin-checks")
    capabilities.register("security/host-checks")
    capabilities.register("security/body-size-cap")
    capabilities.register("security/per-route-body-limits")
