"""Single-include router for ``/cplugapi/v1/*``.

This module is the *only* hook ``modules/api/api.py`` needs into the fork
surface. Keeps upstream-rebase conflicts to one line.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from fastapi import APIRouter, Depends, FastAPI

from . import (
    access_log,
    active_model,
    architectures,
    asyncio_filter,
    capabilities,
    gen_timing,
    health,
    identify,
    idempotency,
    livez_readyz,
    preset,
    queue_endpoint,
    request_id,
    runtime,
    sd_checkpoints,
    sdapi_observer,
    security_middleware,
    session_cancel,
    session_preempt,
    version_endpoint,
)

PREFIX = "/cplugapi/v1"

# Stamped onto ``app.state`` by ``setup_cplugapi`` so a second invocation
# (test reuse, webui reload) returns early instead of double-registering
# routes — FastAPI does not dedupe.
_MOUNT_FLAG = "cplugapi_mounted"
_mount_lock = threading.Lock()


def _register_capabilities() -> None:
    """Advertise what this build wires. Idempotent."""
    capabilities.register("identify")
    capabilities.register("health")
    capabilities.register("version")
    capabilities.register("session/cancel")
    capabilities.register("forge/preset")
    session_preempt.register_capabilities()
    # Audit 02 — Phase A/B observability + hardening.
    security_middleware.register_capabilities()
    idempotency.register_capabilities()
    livez_readyz.register_capabilities()
    queue_endpoint.register_capabilities()
    access_log.register_capabilities()
    gen_timing.register_capabilities()
    sdapi_observer.register_capabilities()
    # Model arch detection (/v1/models/*).
    active_model.register_capabilities()
    sd_checkpoints.register_capabilities()
    architectures.register_capabilities()


def setup_cplugapi(
    app: FastAPI,
    auth_dependency: Optional[Callable] = None,
) -> None:
    """Mount the ``/cplugapi/v1/*`` surface onto ``app``.

    ``auth_dependency`` is a FastAPI dependency callable (typically the
    same Basic-auth checker used by ``/sdapi/v1/*``). When provided it is
    applied to every route EXCEPT ``/identify`` — that endpoint must stay
    unauthenticated so the client can probe a backend before deciding
    whether to send credentials (see Track 05 §5.1).

    Idempotent and thread-safe: concurrent callers serialize on a
    module-level lock so route registration happens at most once per
    ``app`` instance.
    """
    with _mount_lock:
        if getattr(app.state, _MOUNT_FLAG, False):
            return

        runtime.apply_runtime_tweaks()
        gen_timing.install_hooks()
        # Demote benign Windows asyncio connection-reset noise to DEBUG.
        # The desktop client routinely closes connections to preempt
        # in-flight gens; without this filter every preempt logs a
        # WinError-10054 traceback that drowns out real signal.
        asyncio_filter.install()
        _install_middlewares(app)
        _do_mount(app, auth_dependency)
        setattr(app.state, _MOUNT_FLAG, True)


def _install_middlewares(app: FastAPI) -> None:
    """Install the path-scoped middlewares for ``/cplugapi/v1/*``.

    Order matters: Starlette runs the most-recently-added middleware
    first, so we install in *reverse* of the desired runtime order:

    1. idempotency  (added first  → runs last  → wraps the handler)
    2. request_id   (added second → runs middle → stamps state.request_id
                                                 + echoes header on the way out)
    3. security     (added third  → runs middle → rejects bad requests
                                                 before any work happens)
    4. access_log   (added last   → runs first  → measures total
                                                 server-side wall time and
                                                 emits one line per request)

    All four are no-ops outside ``/cplugapi/v1/*`` so ``/sdapi/v1/*``
    byte-identity (CLAUDE.md hard invariant 1) is preserved.

    The individual ``install()`` helpers append to ``app.user_middleware``
    rather than calling ``app.add_middleware`` (which Starlette rejects
    once the app has accepted its first request). After all four are
    registered we rebuild the stack on-the-fly so the new layer is live
    by the time the first ``/cplugapi/v1/*`` request arrives.
    """
    idempotency.install(app)
    request_id.install(app)
    security_middleware.install(app)
    access_log.install(app)
    # /sdapi/v1/* observer — pure ASGI, doesn't share BaseHTTPMiddleware's
    # streaming-response footgun. Installed last so it runs FIRST in the
    # chain (Starlette runs most-recently-added first), giving its
    # dur_ms full coverage of every other layer plus the handler.
    sdapi_observer.install(app)
    # Force a rebuild — Starlette caches the live stack on first request,
    # and ``build_middleware_stack()`` only returns the new stack rather
    # than installing it. Reassign so the next request picks up the new
    # layers.
    app.middleware_stack = app.build_middleware_stack()


def _do_mount(app: FastAPI, auth_dependency: Optional[Callable]) -> None:
    _register_capabilities()

    public = APIRouter()
    private = APIRouter()

    # Public — unauthenticated probes.
    identify.attach(public)

    # Private — inherit /sdapi/v1/* auth posture.
    health.attach(private)
    version_endpoint.attach(private)
    session_cancel.attach(private)
    session_preempt.attach(private)
    preset.attach(private)
    livez_readyz.attach(private)
    queue_endpoint.attach(private)
    active_model.attach(private)
    sd_checkpoints.attach(private)
    architectures.attach(private)

    # Smoke endpoint — Track 05 T17 acceptance. Underscore prefix marks
    # it as implementation-internal (not advertised as a capability).
    @private.get("/_ping")
    def _ping() -> dict:
        return {"ok": True}

    app.include_router(public, prefix=PREFIX)
    if auth_dependency is not None:
        app.include_router(
            private, prefix=PREFIX, dependencies=[Depends(auth_dependency)]
        )
    else:
        app.include_router(private, prefix=PREFIX)
