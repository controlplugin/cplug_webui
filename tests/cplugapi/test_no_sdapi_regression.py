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
    """Calling setup twice (e.g. after webui reload) must not raise
    AND must not double-register routes — FastAPI does not dedupe."""
    app = FastAPI()
    setup_cplugapi(app)
    routes_after_first = len(app.routes)
    setup_cplugapi(app)
    routes_after_second = len(app.routes)
    assert routes_after_first == routes_after_second


def test_setup_is_thread_safe_under_concurrent_call(clean_capabilities):
    """Two threads calling setup_cplugapi on the same app must not
    double-register routes (FastAPI does not dedupe)."""
    import threading

    app = FastAPI()
    barrier = threading.Barrier(8)

    def race():
        barrier.wait()
        setup_cplugapi(app)

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cplug_routes = [r for r in app.routes if hasattr(r, "path") and r.path.startswith(PREFIX)]
    # 6 endpoints (identify, health, version, session/cancel, forge/preset, _ping).
    assert len(cplug_routes) == 6


def test_only_identify_is_unauthenticated(clean_capabilities):
    """Auth-bypass invariant: every cplugapi route except /identify must
    carry the auth dependency when one is supplied. Catches future
    additions that forget to land on the private router."""
    from fastapi import FastAPI

    def reject_all():
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="no")

    app = FastAPI()
    setup_cplugapi(app, auth_dependency=reject_all)

    public_paths = []
    private_paths = []
    for route in app.routes:
        if not hasattr(route, "path") or not route.path.startswith(PREFIX):
            continue
        deps = getattr(route, "dependencies", None) or []
        # If the include_router-level dependency was applied, the route
        # exposes it via Route.dependencies. Identify must NOT have any;
        # everything else MUST have at least one.
        if deps:
            private_paths.append(route.path)
        else:
            public_paths.append(route.path)

    assert public_paths == [f"{PREFIX}/identify"], (
        f"unexpected public routes: {public_paths}"
    )
    # Sanity — at least the Phase 1 endpoints should be on the private side.
    assert any(p.endswith("/health") for p in private_paths)
    assert any(p.endswith("/version") for p in private_paths)
    assert any("/session/cancel/" in p for p in private_paths)
    assert any(p.endswith("/_ping") for p in private_paths)
    assert any("/forge/preset/" in p for p in private_paths)
