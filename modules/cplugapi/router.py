"""Single-include router for ``/cplugapi/v1/*``.

This module is the *only* hook ``modules/api/api.py`` needs into the fork
surface. Keeps upstream-rebase conflicts to one line.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, Depends, FastAPI

from . import (
    capabilities,
    health,
    identify,
    session_cancel,
    version_endpoint,
)

PREFIX = "/cplugapi/v1"


def _register_capabilities() -> None:
    """Advertise what this build wires. Idempotent."""
    capabilities.register("identify")
    capabilities.register("health")
    capabilities.register("version")
    capabilities.register("session/cancel")


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
    """
    _register_capabilities()

    public = APIRouter()
    private = APIRouter()

    # Public — unauthenticated probes.
    identify.attach(public)

    # Private — inherit /sdapi/v1/* auth posture.
    health.attach(private)
    version_endpoint.attach(private)
    session_cancel.attach(private)

    # Smoke endpoint — Track 05 T17 acceptance.
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
