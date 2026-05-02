"""T20 (light) — verify ``/cplugapi/v1/*`` mounting does not break the
``/sdapi/v1/*`` route surface.

This is a unit-level smoke test that doesn't need a model. The full
end-to-end vanilla-A1111 client smoke (real txt2img/img2img against a
small SDXL) is deferred to a CI job that can boot the webui — see
plan/cplugapi-v1.md.
"""

from __future__ import annotations

from fastapi import FastAPI

from modules.cplugapi import PREFIX, setup_cplugapi


def _all_paths(app: FastAPI) -> set[str]:
    return {route.path for route in app.routes if hasattr(route, "path")}


def test_setup_cplugapi_adds_only_cplugapi_routes(clean_capabilities):
    """Mounting cplugapi must not introduce any /sdapi/v1/* route."""
    app = FastAPI()
    before = _all_paths(app)
    setup_cplugapi(app)
    after = _all_paths(app)

    new_routes = after - before
    # Every new route lives under the cplugapi prefix.
    assert all(p.startswith(PREFIX) for p in new_routes), new_routes
    # No sdapi route was touched (defensive — there shouldn't be any to begin with).
    assert not any(p.startswith("/sdapi/") for p in new_routes)


def test_cplugapi_routes_are_what_we_expect(clean_capabilities):
    app = FastAPI()
    setup_cplugapi(app)
    paths = _all_paths(app)
    # Phase 1 deliverables.
    assert f"{PREFIX}/_ping" in paths
    assert f"{PREFIX}/identify" in paths
    assert f"{PREFIX}/health" in paths
    assert f"{PREFIX}/version" in paths
    # Path parameter survives in the registered template.
    assert any(p.startswith(f"{PREFIX}/session/cancel/") for p in paths)


def test_setup_is_idempotent(clean_capabilities):
    """Calling setup twice (e.g. after webui reload) must not raise."""
    app = FastAPI()
    setup_cplugapi(app)
    # Capability-registry idempotence is what protects us here.
    setup_cplugapi(app)
