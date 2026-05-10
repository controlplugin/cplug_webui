"""Token-bucket rate limiting for ``/cplugapi/v1/*`` (W8).

Three classes of buckets, each with its own per-key cap:

- ``mutating`` — POST/PUT/PATCH/DELETE. Default 30 req/min per key.
- ``read`` — GET/HEAD/OPTIONS. Default 600 req/min per key (the desktop
  client polls ``/health`` and ``/queue`` aggressively).
- ``auth_failed`` — counted via the auth-dep wrap installed by
  :func:`observe_auth_failures`. Default 10 req/min — slows credential
  brute force from a single client key.

**Profile-driven defaults**: in the ``desktop`` profile every class is
*off* (the single-user loopback target doesn't need rate limiting and
would just throttle its own polling). In the ``cloud`` profile every
class is *on* with the rates above — the surface is reachable from
the public internet and credential brute force is a live threat.
Operators override per class via ``CPLUG_RATE_LIMIT_MUTATING``,
``_READ``, ``_AUTH_FAILED`` (the value is integer requests-per-minute;
``0`` disables that class explicitly).

Client-key strategy:

- Desktop: ``hash(Authorization)``. On a loopback bind every connection
  shares ``127.0.0.1``, so per-IP keying degenerates. Hashing the
  Authorization header gives concurrent native clients with distinct
  credentials distinct buckets; same-credential clients share —
  acceptable in the single-user posture.
- Cloud: ``parse_xff_real_ip(headers)`` after validating the immediate
  TCP peer is in ``CPLUG_TRUSTED_PROXIES``. Without trusted proxies
  configured, the surface refuses to start in cloud profile —
  XFF-naive servers behind an ingress are a rate-limit bypass, fail-fast
  is the right posture.

429 responses use the W3 problem+json envelope (``code: rate_limited``)
and standard ``Retry-After`` (in seconds, ceiling). Every response also
carries ``X-RateLimit-Limit``, ``X-RateLimit-Remaining``, and
``X-RateLimit-Reset`` (Unix epoch seconds when the bucket next refills).

Path scope: middleware acts only on ``/cplugapi/v1/*``. Outside the
prefix it's a pure passthrough — invariant 1 byte-identity preserved.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import os
import threading
import time
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from . import capabilities, profile
from .errors import CODES, cplugapi_problem

_log = logging.getLogger("cplugapi.rate_limit")
try:
    from backend.logging import setup_logger as _setup_logger

    _setup_logger(_log)
except ImportError:
    pass

_PREFIX = "/cplugapi/v1/"

# Class identifiers — string-typed so they round-trip through metrics
# labels without translation.
CLASS_MUTATING = "mutating"
CLASS_READ = "read"
CLASS_AUTH_FAILED = "auth_failed"

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Env-var names + module defaults. ``0`` is the "off" sentinel
# (per-class disable). The profile chooses which value the *default*
# resolves to — see ``_default_for_class``.
ENV_MUTATING = "CPLUG_RATE_LIMIT_MUTATING"
ENV_READ = "CPLUG_RATE_LIMIT_READ"
ENV_AUTH_FAILED = "CPLUG_RATE_LIMIT_AUTH_FAILED"
ENV_TRUSTED_PROXIES = "CPLUG_TRUSTED_PROXIES"

# Per-class default rates (requests per minute) when active. Cloud
# profile uses these; desktop disables every class.
_CLOUD_DEFAULTS: dict[str, int] = {
    CLASS_MUTATING: 30,
    CLASS_READ: 600,
    CLASS_AUTH_FAILED: 10,
}


def _env_for_class(klass: str) -> str:
    return {
        CLASS_MUTATING: ENV_MUTATING,
        CLASS_READ: ENV_READ,
        CLASS_AUTH_FAILED: ENV_AUTH_FAILED,
    }[klass]


def _default_for_class(klass: str) -> int:
    """Resolve the default rate for ``klass`` based on the active profile.

    Read ``CPLUG_RATE_LIMIT_<CLASS>`` first; if set (including ``0``), it
    wins. If unset, fall back to the profile default: cloud uses
    :data:`_CLOUD_DEFAULTS`, desktop uses ``0`` (off).
    """
    raw = os.environ.get(_env_for_class(klass), "")
    if raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            _log.warning(
                "cplugapi.rate_limit: invalid %s=%r (expected integer); "
                "using profile default", _env_for_class(klass), raw,
            )
    if profile.is_cloud():
        return _CLOUD_DEFAULTS[klass]
    return 0  # desktop: off


# ---------------------------------------------------------------------------
# Token-bucket data structure
# ---------------------------------------------------------------------------


class _Bucket:
    """A single client-key bucket. ``capacity`` is also the per-minute
    rate; refill is continuous (capacity tokens per 60 s)."""

    __slots__ = ("capacity", "tokens", "last_refill")

    def __init__(self, capacity: int, now: float) -> None:
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = now

    def _refill(self, now: float) -> None:
        if now <= self.last_refill:
            return
        elapsed = now - self.last_refill
        # capacity tokens per 60 seconds.
        gained = elapsed * (self.capacity / 60.0)
        self.tokens = min(self.capacity, self.tokens + gained)
        self.last_refill = now

    def take(self, now: float) -> tuple[bool, int, float, float]:
        """Try to consume 1 token. Returns
        ``(consumed, remaining, reset_at, retry_at)``.

        ``consumed`` is True iff a token was actually taken.
        ``remaining`` is the floored token count AFTER the (possible)
        take.
        ``reset_at`` is monotonic seconds when the bucket would be back
        to FULL capacity assuming no further activity. Used for the
        ``X-RateLimit-Reset`` response header (wall-clock equivalent).
        ``retry_at`` is monotonic seconds when one more token will be
        available. Used for the ``Retry-After`` header on 429 — clients
        need to know when the NEXT request can succeed, not when the
        bucket is back to capacity. For high-capacity classes the two
        differ by orders of magnitude (cap=600 read class: full refill
        is 60s, next token is 0.1s).
        """
        self._refill(now)
        consumed = self.tokens >= 1.0
        if consumed:
            self.tokens -= 1.0
        remaining = max(0, math.floor(self.tokens))
        if self.capacity > 0:
            seconds_per_token = 60.0 / self.capacity
            # Time to full refill (capacity tokens).
            deficit = self.capacity - self.tokens
            seconds_to_full = deficit * seconds_per_token
            # Time to one more token: how long until ``tokens`` reaches
            # 1.0. If tokens is already >= 1.0, this is 0.
            seconds_to_one = max(0.0, (1.0 - self.tokens) * seconds_per_token)
        else:
            seconds_to_full = 0.0
            seconds_to_one = 0.0
        return (consumed, remaining, now + seconds_to_full, now + seconds_to_one)

    def peek(self, now: float) -> bool:
        """Return ``True`` iff a take would succeed *without* consuming.

        Used by the auth-dep wrap to short-circuit a bucket-empty
        attempt with 429 before delegating to the inner credential
        check (so a flood of failed credentials gets cheap rejection).
        """
        # Refill is idempotent re: monotonic time, so peek can refill
        # the bucket — it just doesn't decrement.
        self._refill(now)
        return self.tokens >= 1.0


class _ClassRegistry:
    """Holds buckets for one class. Thread-safe — buckets are rebuilt
    on first access and on capacity change (env-var update via
    ``reset_for_test``).
    """

    def __init__(self, klass: str) -> None:
        self._klass = klass
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        # Cache the resolved capacity so ``_default_for_class`` isn't
        # called per-request. Re-read via :func:`reset_for_test` in tests.
        self._capacity: Optional[int] = None

    def _capacity_now(self) -> int:
        if self._capacity is None:
            self._capacity = _default_for_class(self._klass)
        return self._capacity

    def reset_for_test(self) -> None:
        """Drop buckets and recompute capacity from env on next access.

        Test helper — production callers should not rely on this; env
        changes require a webui restart in production."""
        with self._lock:
            self._buckets.clear()
            self._capacity = None

    def take(self, key: str, now: float) -> tuple[bool, int, int, float, float]:
        """Charge 1 token to (klass, key). Returns
        ``(consumed, limit, remaining, reset_at, retry_at)``.

        ``consumed`` is True iff a token was actually taken (i.e. the
        request is allowed). When the class is disabled (capacity == 0),
        returns ``(True, 0, 0, now, now)`` — the caller treats
        consumed-True as "allowed" and ignores the wall-clock outputs.
        ``retry_at`` is monotonic seconds when one more token is
        available — used for the wire ``Retry-After`` header.
        """
        cap = self._capacity_now()
        if cap <= 0:
            return True, 0, 0, now, now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.capacity != cap:
                bucket = _Bucket(cap, now)
                self._buckets[key] = bucket
            consumed, remaining, reset_at, retry_at = bucket.take(now)
            return consumed, cap, remaining, reset_at, retry_at

    def peek(self, key: str, now: float) -> bool:
        """Non-consuming check: would a take succeed right now?

        When capacity is 0 (class disabled), peek always returns True —
        consistent with :meth:`take`'s "always allowed" semantics for
        disabled classes.
        """
        cap = self._capacity_now()
        if cap <= 0:
            return True
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.capacity != cap:
                bucket = _Bucket(cap, now)
                self._buckets[key] = bucket
            return bucket.peek(now)

    def note_failure(self, key: str, now: float) -> None:
        """Record an out-of-band event (e.g. an auth-failed) — consumes
        a token from (klass, key). Used by the auth wrap to charge
        ONLY on 401 results (not on every call)."""
        self.take(key, now)


# ---------------------------------------------------------------------------
# Trusted-proxy / XFF resolution (cloud profile)
# ---------------------------------------------------------------------------


def _trusted_proxy_networks() -> list:
    """Parse ``CPLUG_TRUSTED_PROXIES`` into a list of ip_network objects.
    Empty / invalid entries are dropped with a warning."""
    raw = os.environ.get(ENV_TRUSTED_PROXIES, "")
    if not raw.strip():
        return []
    out = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            out.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            _log.warning(
                "cplugapi.rate_limit: ignoring invalid CIDR in %s: %r",
                ENV_TRUSTED_PROXIES, entry,
            )
    return out


def _client_ip_from_scope(scope: Scope) -> str:
    """Best-effort raw peer IP from the ASGI scope. Uvicorn populates
    ``client = (host, port)`` on HTTP scopes."""
    client = scope.get("client")
    if client and len(client) >= 1:
        return str(client[0])
    return ""


def _real_client_ip(scope: Scope, headers: list[tuple[bytes, bytes]]) -> str:
    """Resolve the client IP for keying.

    Cloud profile: walks ``X-Forwarded-For`` from right to left, skipping
    addresses that are inside any trusted-proxy CIDR. The first untrusted
    address is the real client. If the immediate peer is not itself
    trusted, falls back to the peer (XFF could be forged by the caller).

    Desktop profile: returns the raw peer IP unmodified — no trust chain.
    """
    peer = _client_ip_from_scope(scope)
    if not profile.is_cloud():
        return peer
    trusted = _trusted_proxy_networks()
    if not trusted:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    peer_trusted = any(peer_ip in net for net in trusted)
    if not peer_trusted:
        return peer
    # Walk XFF right-to-left, skipping trusted hops.
    xff_value: Optional[bytes] = None
    for name, value in headers:
        if name == b"x-forwarded-for":
            xff_value = value
            break
    if not xff_value:
        return peer
    candidates = [
        c.strip() for c in xff_value.decode("ascii", errors="ignore").split(",")
    ]
    for c in reversed(candidates):
        if not c:
            continue
        try:
            ip = ipaddress.ip_address(c)
        except ValueError:
            continue
        if any(ip in net for net in trusted):
            continue
        return c
    return peer


def _client_key(scope: Scope) -> str:
    """Resolve the per-request rate-limit key.

    Desktop profile: hash of the Authorization header (or ``"<noauth>"``
    when absent). Distinct credentials → distinct buckets; same
    credential → shared bucket (acceptable single-user posture).

    Cloud profile: real client IP after XFF parsing."""
    headers = scope.get("headers", [])
    if profile.is_cloud():
        ip = _real_client_ip(scope, headers)
        return f"ip:{ip or '<unknown>'}"
    auth_value: Optional[bytes] = None
    for name, value in headers:
        if name == b"authorization":
            auth_value = value
            break
    if not auth_value:
        return "auth:<none>"
    digest = hashlib.sha256(auth_value).hexdigest()[:16]
    return f"auth:{digest}"


# ---------------------------------------------------------------------------
# Class registries
# ---------------------------------------------------------------------------

_mutating = _ClassRegistry(CLASS_MUTATING)
_read = _ClassRegistry(CLASS_READ)
_auth_failed = _ClassRegistry(CLASS_AUTH_FAILED)


def _registry_for_method(method: str) -> _ClassRegistry:
    return _mutating if method.upper() in _MUTATING_METHODS else _read


def reset_for_test() -> None:
    """Test-only — re-read every env var on next request."""
    _mutating.reset_for_test()
    _read.reset_for_test()
    _auth_failed.reset_for_test()


# ---------------------------------------------------------------------------
# Auth-dep wrap — counts 401s for the ``auth_failed`` class
# ---------------------------------------------------------------------------


def observe_auth_failures(auth_dependency: Callable) -> Callable:
    """Wrap an auth callable so 401s increment the auth-failed bucket.

    Intended use in :func:`router.setup_cplugapi`::

        if auth_dependency is not None:
            auth_dependency = rate_limit.observe_auth_failures(auth_dependency)

    The wrap raises ``HTTPException(429)`` BEFORE delegating when the
    auth-failed bucket is empty — i.e. brute-force attempts beyond the
    configured rate are turned away cheaply. Otherwise the wrap delegates
    to the inner ``auth_dependency`` and counts 401 results.

    Keying: at the auth-dep call site we don't have access to the ASGI
    scope, only the ``HTTPBasicCredentials``. Key on
    ``hash(username:password-prefix)`` — distinct credential pairs get
    distinct buckets so a brute-force loop hammering one username doesn't
    affect a legitimate user with a different credential.

    Signature mirroring: FastAPI introspects the auth_dependency's
    parameters when wiring ``Depends(...)``. The wrap MUST mirror the
    inner's parameter shape so the dependency-injection layer keeps
    behaving the same. Two cases:

    - Inner takes ``credentials`` (production case — Forge's
      ``Api.auth(self, credentials: HTTPBasicCredentials = Depends(HTTPBasic()))``):
      we wrap with the same parameter shape and key on the
      credentials.
    - Inner takes no args (test-fixture case — ``def reject_all():``):
      we return the inner unwrapped. There's nothing to key on;
      forcing a fake parameter would change the FastAPI dep-injection
      shape and break the route.
    """
    import inspect

    sig = inspect.signature(auth_dependency)
    # Wrap iff the inner takes at least one positional/keyword arg —
    # that's our credential. Zero-arg callables (test fixtures like
    # ``def reject_all():``) are returned unwrapped so FastAPI's
    # ``Depends`` introspection sees the same shape it would unwrapped.
    positional = [
        p for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
    ]
    if not positional:
        return auth_dependency

    def _key(creds) -> str:
        """Key on **username only**.

        An earlier draft hashed ``username + password-prefix(4)``,
        intending to give legitimate users with the right credential
        their own bucket. Code review (W8 milestone) flagged that
        keying on a partial password lets an attacker varying the
        password past char 4 bypass the rate limit (each variant gets
        a fresh 10/min bucket). Username-only is correct: the legit
        user has username U with the right password and never produces
        401s on their own bucket; an attacker brute-forcing username
        U is throttled regardless of which password they try.
        Different usernames get different buckets, so attacks against
        one user don't lock out others."""
        u = getattr(creds, "username", "") or ""
        return "creds:" + hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]

    def wrapped(credentials):
        now = time.monotonic()
        key = _key(credentials)
        # Peek FIRST (non-consuming): if the bucket is empty, refuse
        # 429 immediately without delegating. This protects against
        # the pre-WB-scrutiny bug where pre-charging on every call
        # throttled legitimate users — peek consumes nothing.
        if not _auth_failed.peek(key, now):
            raise HTTPException(
                status_code=429,
                detail="auth-failure rate exceeded; try again later",
                headers={
                    "Retry-After": "60",
                    "X-Cplug-Error-Code": CODES.RATE_LIMITED,
                },
            )
        try:
            result = auth_dependency(credentials)
        except HTTPException as exc:
            if exc.status_code == 401:
                # Charge the auth-failed bucket — count ONLY on 401.
                # Successful auths don't consume tokens.
                _auth_failed.note_failure(key, now)
            raise
        return result

    # Preserve the inner's signature so FastAPI's Depends() introspection
    # sees the same shape it would have seen unwrapped.
    wrapped.__signature__ = sig  # type: ignore[attr-defined]
    return wrapped


# ---------------------------------------------------------------------------
# Per-request middleware
# ---------------------------------------------------------------------------


def _retry_after_seconds(reset_at: float, now: float) -> int:
    return max(1, int(math.ceil(reset_at - now)))


class CplugapiRateLimitMiddleware:
    """Pure-ASGI rate-limit gate."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not scope.get("path", "").startswith(_PREFIX):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        registry = _registry_for_method(method)
        cap = registry._capacity_now()
        if cap <= 0:
            # Class disabled — no headers, no limit. Pass through.
            await self.app(scope, receive, send)
            return

        key = _client_key(scope)
        now = time.monotonic()
        consumed, limit, remaining, reset_at, retry_at = registry.take(key, now)

        # Compute Unix-epoch reset for the response header. monotonic
        # is the basis for the bucket math, but the wire wants real time.
        wall_now = time.time()
        wall_reset = wall_now + (reset_at - now)
        rl_headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(max(0, remaining)),
            "X-RateLimit-Reset": str(int(wall_reset)),
        }

        if not consumed:
            # Retry-After uses time-to-one-token, NOT time-to-full-refill.
            # For high-capacity classes (e.g. read=600/min cloud default)
            # full-refill is 60s but next-token is 0.1s; clients that
            # honor Retry-After should back off only as long as needed.
            retry_s = _retry_after_seconds(retry_at, now)
            rl_headers["Retry-After"] = str(retry_s)
            # Build 429 problem+json envelope. We need a Request to get
            # request_id but at this point we only have scope. Pull
            # X-Request-Id directly from headers (request_id middleware
            # may not have run yet at this layer's outermost position).
            rid = None
            for name, value in scope.get("headers", []):
                if name == b"x-request-id":
                    rid = value.decode("ascii", errors="ignore")
                    break
            class_name = "mutating" if method in _MUTATING_METHODS else "read"
            response = cplugapi_problem(
                status=429,
                code=CODES.RATE_LIMITED,
                detail=f"rate limit exceeded for class={class_name}; retry after {retry_s}s",
                request_id=rid,
                headers=rl_headers,
            )
            # Send the response manually since we're a pure-ASGI shim.
            await response(scope, receive, send)
            return

        # Allowed — wrap send to inject rate-limit headers on the way out.
        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for k, v in rl_headers.items():
                    headers.append((k.encode("ascii"), v.encode("ascii")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped_send)


# ---------------------------------------------------------------------------
# Wire-up
# ---------------------------------------------------------------------------

_INSTALL_FLAG = "cplugapi_rate_limit_installed"
_install_lock = threading.Lock()


def install(app: FastAPI) -> None:
    """Attach the rate-limit middleware. Idempotent + thread-safe.

    Inserted at the front of ``user_middleware`` so it runs early in the
    chain — rejecting at this layer avoids the cost of every other
    middleware downstream. Per the canonical ordering invariant
    documented in ``plan/cplugapi-world-class.md`` §3.0, rate_limit
    sits between access_log (outermost) and security.
    """
    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(0, Middleware(CplugapiRateLimitMiddleware))
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Always advertise; class-specific activation is operator policy
    (env vars / profile). The capability tells clients the *mechanism*
    is wired."""
    capabilities.register("security/rate-limit")
    # Per-class advertising requires reading env at register time;
    # currently disabled per-class state surfaces only via 429s and the
    # X-RateLimit-Limit header. Sufficient for client behaviour.


# ---------------------------------------------------------------------------
# Cloud profile fail-fast: refuse to start without trusted proxies
# ---------------------------------------------------------------------------


def validate_startup() -> None:
    """Cloud-profile sanity check.

    When ``cloud`` profile is active AND any rate-limit class is enabled,
    ``CPLUG_TRUSTED_PROXIES`` MUST be configured — otherwise the rate
    limit is trivially bypassable (the caller controls XFF). Raise at
    startup so the operator sees the misconfig before traffic hits.

    Desktop profile bypasses the check (rate limit is off by default
    anyway; if the operator explicitly enables a class on desktop they
    own the consequences).

    Pytest bypass: when ``PYTEST_CURRENT_TEST`` is set (auto-set by
    pytest per-test), the validation is skipped — tests that activate
    the cloud profile to exercise other behaviour don't have to set
    trusted proxies just to satisfy this gate. Tests that explicitly
    cover the validation logic invoke it directly.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if not profile.is_cloud():
        return
    any_enabled = any(
        _default_for_class(c) > 0
        for c in (CLASS_MUTATING, CLASS_READ, CLASS_AUTH_FAILED)
    )
    if not any_enabled:
        return
    if not _trusted_proxy_networks():
        raise RuntimeError(
            "cplugapi rate-limit: cloud profile is active and at least one "
            "rate-limit class is enabled, but CPLUG_TRUSTED_PROXIES is "
            "unset. Set it to a CSV of trusted-proxy CIDRs (e.g. "
            "'10.0.0.0/8,172.16.0.0/12') so X-Forwarded-For parsing is "
            "safe; otherwise set every CPLUG_RATE_LIMIT_* var to 0 to "
            "disable rate limiting."
        )
