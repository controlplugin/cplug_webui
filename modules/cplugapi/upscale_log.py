"""Tagged log line for upscale requests on ``/sdapi/v1/*``.

The desktop client supports two post-process upscale flows
(documented in the client's ``generation.rs``):

1. **Extras** — ``POST /sdapi/v1/extra-single-image``. Single-shot
   ESRGAN/SwinIR upscale. Distinct endpoint, so we can detect it
   server-side without help.
2. **Img2Img refine** — ``POST /sdapi/v1/img2img`` with the gen body
   width/height set to source-dims × scale and a low ``denoising_strength``.
   Shares the endpoint with ordinary img2img, so we can't tell it apart
   from the body without heuristic gymnastics. The client tags it with
   the ``X-Cplug-Intent: upscale`` header (or ``Upscale-Img2Img`` /
   ``upscale`` truthy variants); we log only when the header is present.

Why a dedicated module rather than folding into :mod:`sdapi_observer`:
the observer is a generic "every request" line — useful but verbose
when the operator just wants to spot upscales. Splitting them lets an
operator run with the upscale log on (low-frequency, high-signal) and
the full sdapi observer off.

Pure ASGI for the same reason as :mod:`sdapi_observer` — Forge's
upstream surface includes streaming endpoints under ``/sdapi/`` and
``BaseHTTPMiddleware`` interacts badly with them
(encode/starlette#1438). We never touch ``send`` or the body, just
sniff scope (path + headers) and forward.

**Read-only** — never mutates request or response bytes. Preserves the
``/sdapi/v1/*`` byte-identity invariant (CLAUDE.md §1).

Env-var toggle: ``CPLUG_UPSCALE_LOG`` (default ON — frequency is low
enough that the line doesn't flood the console even during heavy
upscaling sessions). Read once at install time.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from fastapi import FastAPI

from . import capabilities

_log = logging.getLogger("cplugapi.upscale")
try:
    from backend.logging import setup_logger as _setup_logger
    _setup_logger(_log)
except ImportError:
    pass  # OpenAPI export / tests stub backend out

_ENV_DISABLE = "CPLUG_UPSCALE_LOG"

# Path that ALWAYS counts as an upscale (different endpoint from
# generation, so detection is unambiguous).
_EXTRAS_PATH = "/sdapi/v1/extra-single-image"

# Path that conditionally counts as an upscale (shares with ordinary
# img2img; depends on header).
_IMG2IMG_PATH = "/sdapi/v1/img2img"

# Header the desktop client sets on its Img2Img-refine upscale flow.
# Lowercase compare in ASGI because scope headers are bytes pairs and
# HTTP headers are case-insensitive by spec.
_INTENT_HEADER = b"x-cplug-intent"
_UPSCALE_VALUES: frozenset[bytes] = frozenset((
    b"upscale",
    b"upscale-img2img",
    b"upscale-refine",
))


def _is_enabled() -> bool:
    """``CPLUG_UPSCALE_LOG=0`` (or false/no/off) disables emission."""
    raw = os.environ.get(_ENV_DISABLE)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _content_length_from_scope(scope) -> int:
    """Return Content-Length as int, or -1 when absent / non-numeric."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return -1
    return -1


def _has_upscale_intent_header(scope) -> bool:
    """True iff the request carries ``X-Cplug-Intent`` with an upscale value.

    Compared bytes-vs-bytes to avoid the per-request decode cost. The
    header values we accept are documented at the module top — extending
    the set is a one-line edit.
    """
    for name, value in scope.get("headers", []):
        if name == _INTENT_HEADER:
            return value.strip().lower() in _UPSCALE_VALUES
    return False


def _classify_path(scope) -> Optional[str]:
    """Return ``"extras"`` / ``"img2img-refine"`` for known upscale
    paths, or ``None`` to skip.

    The img2img path is conditional on the intent header: a regular
    sketch-driven img2img also lands here and we don't want to tag it
    as an upscale.
    """
    path = scope.get("path", "")
    if path == _EXTRAS_PATH:
        return "extras"
    if path == _IMG2IMG_PATH and _has_upscale_intent_header(scope):
        return "img2img-refine"
    return None


class UpscaleRequestLogger:
    """Pure-ASGI middleware that emits one tagged line per upscale.

    Outside the matched paths (or when no intent header is present on
    img2img) the middleware is a straight pass-through, no log lines,
    no overhead.
    """

    def __init__(self, app, enabled: bool = True) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        kind = _classify_path(scope)
        if kind is None:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "")
        in_bytes = _content_length_from_scope(scope)

        # Pre-handler emission — operators can correlate the line with
        # the request that triggered it before the handler returns. The
        # gen_timing log will surface per-gen wall time for img2img-refine
        # (Extras goes through ``run_postprocessing``, which gen_timing
        # doesn't wrap; the Extras line is the only log signal for the
        # extras case).
        _log.info(
            "upscale request: type=%s %s %s in=%d",
            kind, method, path, in_bytes,
            extra={
                "upscale_type": kind,
                "method": method,
                "path": path,
                "in_bytes": in_bytes,
            },
        )

        await self.app(scope, receive, send)


_INSTALL_FLAG = "cplugapi_upscale_log_installed"
_install_lock = threading.Lock()


def install(app: FastAPI) -> None:
    """Attach the middleware to ``app``. Idempotent + thread-safe.

    Always installs so the enabled-state can flip across test runs
    without re-mounting; runtime gating is via the ``enabled`` flag
    captured at install time from :func:`_is_enabled`.

    Inserted at position 0 of ``user_middleware`` so it runs OUTERMOST
    in the chain. Caller (router) is responsible for invoking
    ``app.build_middleware_stack()`` once registration is done.
    """
    from starlette.middleware import Middleware

    with _install_lock:
        if getattr(app.state, _INSTALL_FLAG, False):
            return
        app.user_middleware.insert(
            0,
            Middleware(UpscaleRequestLogger, enabled=_is_enabled()),
        )
        setattr(app.state, _INSTALL_FLAG, True)


def register_capabilities() -> None:
    """Advertise the upscale-request log when enabled.

    W15 — dual-emits ``observability/upscale-log`` (new) and
    ``upscale-log`` (legacy)."""
    if _is_enabled():
        capabilities.register_with_legacy(
            new_name="observability/upscale-log",
            legacy_name="upscale-log",
        )
