"""Tests for ``modules.cplugapi.tracing`` — W3C traceparent propagation (W11).

The middleware is pure-ASGI (not ``BaseHTTPMiddleware``) so it can be
exercised with a real ``TestClient`` for the happy paths AND directly
with mock ASGI scopes for the edge cases (WebSocket pass-through,
out-of-prefix pass-through). Both styles are used here.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, setup_cplugapi, tracing


_VALID_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_client_with(extra_attach):
    """Mount cplugapi + install tracing + apply per-test router attach.

    ``router.py`` may not (yet) call ``tracing.install`` itself — the
    user wires that — so the test installs explicitly. Once the router
    wires tracing the explicit install becomes a no-op (idempotent)."""
    app = FastAPI()
    setup_cplugapi(app)
    tracing.install(app)
    # Rebuild the middleware stack so the freshly-added layer is live.
    app.middleware_stack = app.build_middleware_stack()
    extra = APIRouter()
    extra_attach(extra)
    app.include_router(extra, prefix=PREFIX)
    return TestClient(app)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _RecordingSend:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


# ---------------------------------------------------------------------------
# unit-level: validator + generator
# ---------------------------------------------------------------------------


def test_generate_traceparent_matches_w3c_spec():
    """Generated traceparent must be parseable by the spec regex and
    have non-zero trace-id + parent-id."""
    for _ in range(20):
        tp = tracing._generate_traceparent()
        assert _VALID_RE.match(tp), tp
        # Validator must accept what the generator produced.
        parsed = tracing._validate_traceparent(tp)
        assert parsed is not None, tp
        version, trace_id, parent_id, flags = parsed
        assert version == "00"
        assert trace_id != "0" * 32
        assert parent_id != "0" * 16
        assert flags == "00"


def test_generate_traceparent_is_unique():
    """Two consecutive generations must yield distinct trace ids."""
    seen = {tracing._generate_traceparent() for _ in range(32)}
    assert len(seen) == 32


def test_validate_accepts_well_formed():
    tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    parsed = tracing._validate_traceparent(tp)
    assert parsed == (
        "00",
        "0af7651916cd43dd8448eb211c80319c",
        "b7ad6b7169203331",
        "01",
    )


def test_validate_rejects_wrong_shape():
    """Various malformed inputs must all return None."""
    bad = [
        "",
        "garbage",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331",  # missing flags
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-extra",
        "00-0af7651916cd43dd8448eb211c80319-b7ad6b7169203331-01",   # 31 chars
        "00-0af7651916cd43dd8448eb211c80319cz-b7ad6b7169203331-01", # non-hex
        "00 0af7651916cd43dd8448eb211c80319c b7ad6b7169203331 01",  # spaces
        "FF-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",  # upper-case A-F
    ]
    for value in bad:
        assert tracing._validate_traceparent(value) is None, value


def test_validate_rejects_all_zero_trace_id():
    """Per W3C spec all-zero trace-id is invalid even though shape matches."""
    tp = "00-" + "0" * 32 + "-b7ad6b7169203331-01"
    assert tracing._validate_traceparent(tp) is None


def test_validate_rejects_all_zero_parent_id():
    """Per W3C spec all-zero parent-id is invalid even though shape matches."""
    tp = "00-0af7651916cd43dd8448eb211c80319c-" + "0" * 16 + "-01"
    assert tracing._validate_traceparent(tp) is None


# ---------------------------------------------------------------------------
# integration: middleware behaviour via TestClient
# ---------------------------------------------------------------------------


def test_traceparent_generated_when_header_absent(clean_capabilities):
    """No inbound traceparent → middleware mints one, exposes it on
    ``request.state``, and echoes it on the response."""
    captured: dict = {}

    def attach(r: APIRouter) -> None:
        @r.get("/_tp_probe")
        def probe(request: Request) -> dict:
            captured["tp"] = tracing.get_traceparent(request)
            captured["tid"] = tracing.get_trace_id(request)
            return {"ok": True}

    client = _make_client_with(attach)
    r = client.get(f"{PREFIX}/_tp_probe")

    assert r.status_code == 200
    tp = r.headers["traceparent"]
    assert _VALID_RE.match(tp)
    assert captured["tp"] == tp
    # trace_id is the second segment.
    assert captured["tid"] == tp.split("-")[1]
    # 32-char hex.
    assert len(captured["tid"]) == 32


def test_inbound_valid_traceparent_echoed(clean_capabilities):
    """Valid inbound traceparent is preserved verbatim on the response
    AND on ``request.state``."""
    captured: dict = {}

    def attach(r: APIRouter) -> None:
        @r.get("/_tp_probe")
        def probe(request: Request) -> dict:
            captured["tp"] = tracing.get_traceparent(request)
            captured["tid"] = tracing.get_trace_id(request)
            return {"ok": True}

    inbound = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    client = _make_client_with(attach)
    r = client.get(f"{PREFIX}/_tp_probe", headers={"traceparent": inbound})

    assert r.status_code == 200
    assert r.headers["traceparent"] == inbound
    assert captured["tp"] == inbound
    assert captured["tid"] == "0af7651916cd43dd8448eb211c80319c"


def test_inbound_malformed_traceparent_replaced(clean_capabilities):
    """Wrong-shape inbound is silently replaced — the request still
    succeeds and gets a fresh canonical traceparent."""
    captured: dict = {}

    def attach(r: APIRouter) -> None:
        @r.get("/_tp_probe")
        def probe(request: Request) -> dict:
            captured["tp"] = tracing.get_traceparent(request)
            return {"ok": True}

    bogus = "this-is-not-a-traceparent"
    client = _make_client_with(attach)
    r = client.get(f"{PREFIX}/_tp_probe", headers={"traceparent": bogus})

    assert r.status_code == 200
    out = r.headers["traceparent"]
    assert out != bogus
    assert _VALID_RE.match(out)
    assert captured["tp"] == out


def test_inbound_all_zero_trace_id_replaced(clean_capabilities):
    """All-zero trace-id is invalid per W3C; middleware mints a fresh one."""
    def attach(r: APIRouter) -> None:
        @r.get("/_tp_probe")
        def probe() -> dict:
            return {"ok": True}

    bad = "00-" + "0" * 32 + "-b7ad6b7169203331-01"
    client = _make_client_with(attach)
    r = client.get(f"{PREFIX}/_tp_probe", headers={"traceparent": bad})

    assert r.status_code == 200
    out = r.headers["traceparent"]
    assert out != bad
    assert _VALID_RE.match(out)
    assert out.split("-")[1] != "0" * 32


def test_response_carries_traceparent_header(clean_capabilities):
    """Smoke check: every cplugapi response gets a traceparent header."""
    def attach(r: APIRouter) -> None:
        @r.get("/_tp_probe")
        def probe() -> dict:
            return {"ok": True}

    client = _make_client_with(attach)
    r = client.get(f"{PREFIX}/_tp_probe")
    assert "traceparent" in r.headers


def test_does_not_apply_outside_prefix(clean_capabilities):
    """Routes outside ``/cplugapi/v1/*`` must not get a traceparent
    header — invariant 1 (``/sdapi/v1/*`` byte-identity)."""
    app = FastAPI()
    setup_cplugapi(app)
    tracing.install(app)
    app.middleware_stack = app.build_middleware_stack()

    @app.get("/sdapi/v1/_test/foo")
    def fake() -> dict:
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/sdapi/v1/_test/foo")
    assert r.status_code == 200
    assert "traceparent" not in r.headers


def test_does_not_apply_outside_prefix_even_with_inbound(clean_capabilities):
    """An inbound traceparent on a non-cplugapi route must NOT appear on
    the response — out-of-prefix is full pass-through."""
    app = FastAPI()
    setup_cplugapi(app)
    tracing.install(app)
    app.middleware_stack = app.build_middleware_stack()

    @app.get("/sdapi/v1/_test/foo")
    def fake() -> dict:
        return {"ok": True}

    client = TestClient(app)
    r = client.get(
        "/sdapi/v1/_test/foo",
        headers={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
    )
    # No echo — the middleware short-circuited on the path check.
    assert "traceparent" not in r.headers


# ---------------------------------------------------------------------------
# direct ASGI-level edge cases
# ---------------------------------------------------------------------------


def test_websocket_scope_passes_through_unchanged(clean_capabilities):
    """The middleware never touches WebSocket scopes — keeps WS upgrades
    unaffected so a future T31 endpoint isn't disturbed."""
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope["type"])

    mw = tracing.CplugapiTracingMiddleware(inner)
    scope = {
        "type": "websocket",
        "path": "/cplugapi/v1/_test/ws",
        "headers": [],
    }
    send = _RecordingSend()
    _run(mw(scope, lambda: None, send))

    assert inner_called == ["websocket"]
    # The middleware did not synthesise any messages itself.
    assert send.messages == []


def test_lifespan_scope_passes_through(clean_capabilities):
    """Non-http / non-websocket scopes (e.g. ``lifespan``) must pass
    through. ``scope.get('path', '')`` doesn't apply so we exercise the
    early ``type != 'http'`` short-circuit."""
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope["type"])

    mw = tracing.CplugapiTracingMiddleware(inner)
    scope = {"type": "lifespan"}
    _run(mw(scope, lambda: None, _RecordingSend()))
    assert inner_called == ["lifespan"]


def test_install_is_idempotent(clean_capabilities):
    """A second install must not stack the middleware."""
    app = FastAPI()
    setup_cplugapi(app)
    tracing.install(app)
    n1 = len(app.user_middleware)
    tracing.install(app)
    n2 = len(app.user_middleware)
    assert n1 == n2


# ---------------------------------------------------------------------------
# Capability registration
# ---------------------------------------------------------------------------


def test_capability_registered(clean_capabilities):
    """``register_capabilities()`` advertises the W3C trace-context
    feature. The router-level wiring is the user's responsibility — this
    test verifies the capability string is what the router will pick up
    once it calls into this module."""
    from modules.cplugapi import capabilities

    tracing.register_capabilities()
    assert "observability/trace-context-w3c" in capabilities.enabled_capabilities()
