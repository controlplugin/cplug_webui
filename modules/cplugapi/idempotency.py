"""Stripe-style ``Idempotency-Key`` middleware for ``/cplugapi/v1/*``.

Mutating endpoints under the fork surface (POST/PUT/PATCH/DELETE) accept
an ``Idempotency-Key`` header. The middleware caches the first response
(status + headers + body) per ``(method, path, key)`` triple so retries
arriving on a flaky transport replay the original outcome with an
``Idempotency-Replayed: true`` marker. This is the standard contract
documented at <https://stripe.com/docs/api/idempotent_requests>.

Design choices:

* Cache 2xx **and** 4xx — a client retrying after a 422 must see the
  same validation error rather than a different one mid-flight.
* In-memory LRU with TTL. Sized via env vars so an operator can tune
  for memory pressure without code changes:

    - ``CPLUG_IDEMPOTENCY_MAX``    (entry cap, default 1024)
    - ``CPLUG_IDEMPOTENCY_TTL_S``  (TTL seconds, default 86400 = 24 h)

* Path-scoped to ``/cplugapi/v1/*`` so ``/sdapi/v1/*`` stays
  byte-identical with upstream (CLAUDE.md hard invariant 1).
* Key shape: an opaque ASCII token, 8-128 chars, characters from the
  UUID / ULID / base64url alphabet plus ``-_:.``. Anything outside that
  is rejected with 400 — keeps log noise and storage attacks bounded.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import capabilities

HEADER_KEY = "Idempotency-Key"
HEADER_REPLAYED = "Idempotency-Replayed"

_PREFIX = "/cplugapi/v1"
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 8 chars covers short ULIDs / nanoids; 128 is a safe ceiling versus a
# malicious "Idempotency-Key: " + 64 KiB blob trying to amplify storage.
_KEY_MIN_LEN = 8
_KEY_MAX_LEN = 128
# Whitelist alphabet: UUID + base64url + ULID-friendly punctuation.
_KEY_REGEX = re.compile(r"^[A-Za-z0-9_:.\-]+$")


def _read_int_env(name: str, default: int) -> int:
    """Read a non-negative int from env. Bad value -> default + warn."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value <= 0:
            return default
        return value
    except ValueError:
        return default


def _config_max() -> int:
    return _read_int_env("CPLUG_IDEMPOTENCY_MAX", 1024)


def _config_ttl() -> int:
    return _read_int_env("CPLUG_IDEMPOTENCY_TTL_S", 86400)


class _CacheEntry:
    __slots__ = ("status", "headers", "body", "media_type", "stored_at")

    def __init__(
        self,
        status: int,
        headers: list[tuple[str, str]],
        body: bytes,
        media_type: Optional[str],
        stored_at: float,
    ) -> None:
        self.status = status
        # ``headers`` is a list of (name, value) tuples preserving order +
        # duplicates (e.g. ``Set-Cookie``) — exactly what Starlette gives
        # us via ``response.raw_headers``.
        self.headers = headers
        self.body = body
        self.media_type = media_type
        self.stored_at = stored_at


class _LruCache:
    """Bounded LRU with TTL. Bounds re-read on each access so
    env-var-driven changes take effect without a process restart."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[tuple[str, str, str], _CacheEntry]" = OrderedDict()

    def get(self, key: tuple[str, str, str]) -> Optional[_CacheEntry]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            now = time.monotonic()
            if now - entry.stored_at > _config_ttl():
                # Expired; drop it so callers see a clean miss.
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: tuple[str, str, str], entry: _CacheEntry) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._evict_locked()

    def _evict_locked(self) -> None:
        cap = _config_max()
        while len(self._entries) > cap:
            self._entries.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


_cache = _LruCache()


def reset_cache() -> None:
    """Test-only: clear the idempotency cache."""
    _cache.reset()


def cache_size() -> int:
    """Test-only: current cache occupancy."""
    return _cache.size()


def _validate_key(key: str) -> bool:
    if not (_KEY_MIN_LEN <= len(key) <= _KEY_MAX_LEN):
        return False
    return _KEY_REGEX.match(key) is not None


def _replay(entry: _CacheEntry) -> Response:
    """Reconstruct a Response from a cache entry and tag it as replayed."""
    response = Response(
        content=entry.body,
        status_code=entry.status,
        media_type=entry.media_type,
    )
    # Restore the original headers (preserving duplicates / order). We
    # explicitly skip Content-Length — Response.__init__ already wrote
    # one based on ``entry.body`` — and any pre-existing
    # Idempotency-Replayed header so we can set our own canonical value.
    # ``raw_headers`` is a list of (bytes, bytes) tuples; entries are
    # stored as (str, str) for cache-survivability so we re-encode here.
    skip = {"content-length", HEADER_REPLAYED.lower()}
    response.raw_headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for (name, value) in entry.headers
        if name.lower() not in skip
    ]
    response.headers[HEADER_REPLAYED] = "true"
    return response


class CplugapiIdempotencyMiddleware(BaseHTTPMiddleware):
    """Cache + replay responses keyed on ``Idempotency-Key``."""

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(_PREFIX):
            return await call_next(request)
        if request.method.upper() not in _MUTATING_METHODS:
            return await call_next(request)

        key = request.headers.get(HEADER_KEY)
        if not key:
            return await call_next(request)

        if not _validate_key(key):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_idempotency_key",
                    "detail": (
                        f"Idempotency-Key must be {_KEY_MIN_LEN}-{_KEY_MAX_LEN} "
                        "ASCII chars from [A-Za-z0-9_:.-]"
                    ),
                },
            )

        cache_key = (request.method.upper(), request.url.path, key)
        cached = _cache.get(cache_key)
        if cached is not None:
            return _replay(cached)

        response: Response = await call_next(request)

        # Only cache 2xx and 4xx — 5xx are server errors, replaying them
        # would mask transient issues that subsequent retries should
        # legitimately see resolve. 3xx redirects are not currently used
        # by the fork surface; pass through without caching.
        status = response.status_code
        if not (200 <= status < 300 or 400 <= status < 500):
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        # Capture the raw headers as (name, value) string tuples so the
        # cache survives across event-loop iterations even if Starlette
        # reuses the underlying byte buffers.
        captured_headers: list[tuple[str, str]] = []
        for raw_name, raw_value in response.raw_headers:
            name = raw_name.decode("latin-1") if isinstance(raw_name, bytes) else raw_name
            value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else raw_value
            captured_headers.append((name, value))

        entry = _CacheEntry(
            status=status,
            headers=captured_headers,
            body=body,
            media_type=response.media_type,
            stored_at=time.monotonic(),
        )
        _cache.put(cache_key, entry)

        # Build a fresh response from the captured body so the client
        # receives the same bytes the cache will replay later.
        rebuilt = Response(
            content=body,
            status_code=status,
            media_type=response.media_type,
        )
        skip = {"content-length"}
        rebuilt.raw_headers = [
            (n.encode("latin-1"), v.encode("latin-1"))
            for (n, v) in captured_headers
            if n.lower() not in skip
        ]
        return rebuilt


_INSTALL_FLAG = "cplugapi_idempotency_installed"
_install_lock = threading.Lock()


def install(app: FastAPI) -> None:
    """Attach the middleware to ``app``. Idempotent + thread-safe."""
    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.add_middleware(CplugapiIdempotencyMiddleware)
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Advertise idempotency support. Idempotent."""
    capabilities.register("idempotency")
