"""Tests for ``modules.cplugapi.request_id`` middleware."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, request_id, setup_cplugapi


def _make_client_with(extra_attach):
    """Mount cplugapi + apply per-test middleware/router attach."""
    app = FastAPI()
    setup_cplugapi(app)
    request_id.install(app)
    extra = APIRouter()
    extra_attach(extra)
    app.include_router(extra, prefix=PREFIX)
    return TestClient(app)


def test_generated_when_header_absent(clean_capabilities):
    captured: dict = {}

    def attach(r: APIRouter) -> None:
        @r.get("/_rid_probe")
        def probe(request: Request) -> dict:
            captured["rid"] = request_id.get_request_id(request)
            return {"ok": True}

    client = _make_client_with(attach)
    r = client.get(f"{PREFIX}/_rid_probe")

    assert r.status_code == 200
    rid = r.headers["X-Request-Id"]
    assert rid.startswith("req_")
    # 12 random bytes -> 16 base64url chars after the prefix.
    assert len(rid) > len("req_")
    # Handler saw the same id.
    assert captured["rid"] == rid


def test_inbound_header_echoed(clean_capabilities):
    def attach(r: APIRouter) -> None:
        @r.get("/_rid_probe")
        def probe(request: Request) -> dict:
            return {"id": request_id.get_request_id(request)}

    client = _make_client_with(attach)
    inbound = "req_clientsupplied123"
    r = client.get(
        f"{PREFIX}/_rid_probe", headers={"X-Request-Id": inbound}
    )
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == inbound
    assert r.json() == {"id": inbound}


def test_does_not_apply_outside_prefix(clean_capabilities):
    """Routes outside ``/cplugapi/v1/*`` must not get the header — that
    would breach the ``/sdapi/v1/*`` byte-identity invariant."""
    app = FastAPI()
    setup_cplugapi(app)
    request_id.install(app)

    @app.get("/sdapi/fake")
    def fake() -> dict:
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/sdapi/fake")
    assert r.status_code == 200
    assert "X-Request-Id" not in r.headers


def test_install_is_idempotent(clean_capabilities):
    """Calling install twice must not stack the middleware."""
    app = FastAPI()
    setup_cplugapi(app)
    request_id.install(app)
    middleware_count_first = len(app.user_middleware)
    request_id.install(app)
    middleware_count_second = len(app.user_middleware)
    assert middleware_count_first == middleware_count_second


def test_generated_id_is_unique_across_requests(clean_capabilities):
    def attach(r: APIRouter) -> None:
        @r.get("/_rid_probe")
        def probe() -> dict:
            return {"ok": True}

    client = _make_client_with(attach)
    seen = set()
    for _ in range(8):
        r = client.get(f"{PREFIX}/_rid_probe")
        seen.add(r.headers["X-Request-Id"])
    # 8 separate calls, no inbound header -> 8 distinct generated ids.
    assert len(seen) == 8


def test_get_request_id_returns_none_when_missing():
    """Helper returns None on a Request that never went through the
    middleware (e.g. a unit-test direct call)."""
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/somewhere",
        "headers": [],
    }
    req = StarletteRequest(scope)
    assert request_id.get_request_id(req) is None
