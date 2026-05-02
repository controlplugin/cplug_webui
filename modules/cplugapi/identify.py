"""``GET /cplugapi/v1/identify`` — Track 05 §5.1.

The cheapest possible probe: lets the desktop client distinguish the
ControlPlugin_WebUI fork from upstream Forge Neo / Reforge / vanilla A1111
without inflicting state on the backend. **Unauthenticated** — bootstrap
chicken-and-egg means the client must identify before it knows whether to
send credentials.
"""

from __future__ import annotations

from fastapi import APIRouter

from .__version__ import (
    FORK_COMMIT,
    FORK_NAME,
    FORK_VERSION,
    UPSTREAM_COMMIT,
    UPSTREAM_NAME,
)


def attach(router: APIRouter) -> None:
    @router.get("/identify")
    def identify() -> dict:
        return {
            "fork": FORK_NAME,
            "fork_version": FORK_VERSION,
            "fork_commit": FORK_COMMIT,
            "upstream": UPSTREAM_NAME,
            "upstream_commit": UPSTREAM_COMMIT,
        }
