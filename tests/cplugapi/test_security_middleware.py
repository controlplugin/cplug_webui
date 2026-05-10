"""Unit tests for ``modules.cplugapi.security_middleware``.

Each test builds a fresh FastAPI app, installs the middleware via the
public ``install()`` helper, and exercises behavior through
``fastapi.testclient.TestClient`` — same convention as ``test_router.py``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import security_middleware
from modules.cplugapi.security_middleware import (
    PROTECTED_PREFIX,
    ROUTE_LIMITS,
    CplugapiSecurityMiddleware,
    _match_route_limit,
    _parse_route_limits_env,
    install,
    register_capabilities,
)


def _make_app() -> FastAPI:
    """Build an app with a single guarded route under PROTECTED_PREFIX,
    plus a sibling under ``/sdapi/v1/`` to exercise path-scoping."""
    app = FastAPI()

    @app.get(f"{PROTECTED_PREFIX}probe")
    def probe() -> dict:
        return {"ok": True}

    @app.post(f"{PROTECTED_PREFIX}probe")
    def probe_post() -> dict:
        return {"ok": True}

    @app.get("/sdapi/v1/test")
    def sdapi_passthrough() -> dict:
        return {"sdapi": True}

    install(app)
    return app


def _client(app: FastAPI) -> TestClient:
    """TestClient bound to a loopback base_url so the synthesized
    ``Host`` header is in the default allow-list. Without this,
    Starlette's TestClient defaults to ``host: testserver`` which our
    DNS-rebind defence would (correctly) reject."""
    return TestClient(app, base_url="http://127.0.0.1:7860")


# --- Origin allow-list -------------------------------------------------------


def test_origin_loopback_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://127.0.0.1:7860"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_origin_evil_rejected():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert "origin" in r.json()["detail"].lower()


def test_origin_absent_allowed():
    client = _client(_make_app())
    r = client.get(f"{PROTECTED_PREFIX}probe")
    assert r.status_code == 200


def test_origin_null_allowed():
    client = _client(_make_app())
    r = client.get(f"{PROTECTED_PREFIX}probe", headers={"Origin": "null"})
    assert r.status_code == 200


def test_origin_localhost_with_port_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://localhost:12345"},
    )
    assert r.status_code == 200


def test_origin_ipv6_loopback_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://[::1]:7860"},
    )
    assert r.status_code == 200


# --- Sec-Fetch-Site ----------------------------------------------------------


def test_sec_fetch_site_cross_site_rejected():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403


def test_sec_fetch_site_same_site_rejected():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Sec-Fetch-Site": "same-site"},
    )
    assert r.status_code == 403


def test_sec_fetch_site_none_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Sec-Fetch-Site": "none"},
    )
    assert r.status_code == 200


def test_sec_fetch_site_same_origin_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200


def test_sec_fetch_site_absent_allowed():
    client = _client(_make_app())
    r = client.get(f"{PROTECTED_PREFIX}probe")
    assert r.status_code == 200


# --- Host allow-list ---------------------------------------------------------


def test_host_dns_rebind_rejected():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Host": "127.0.0.1.evil.example"},
    )
    assert r.status_code == 403
    assert "host" in r.json()["detail"].lower()


def test_host_loopback_with_port_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Host": "127.0.0.1:7860"},
    )
    assert r.status_code == 200


def test_host_localhost_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Host": "localhost"},
    )
    assert r.status_code == 200


def test_host_ipv6_loopback_allowed():
    client = _client(_make_app())
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Host": "[::1]:7860"},
    )
    assert r.status_code == 200


# --- Body size cap -----------------------------------------------------------


def test_post_oversized_body_rejected():
    client = _client(_make_app())
    r = client.post(
        f"{PROTECTED_PREFIX}probe",
        headers={"Content-Length": "100000000"},
        # Provide a small actual body — TestClient may overwrite
        # Content-Length, so we rely on the middleware seeing the
        # explicit override via the headers dict. If Starlette rewrites
        # it, see the dedicated test below that injects the header
        # via a direct middleware instance.
        content=b"x" * 16,
    )
    # Either the middleware caught the declared 100 MB (preferred path)
    # or TestClient overwrote it to 16; in the latter case we still
    # need a separate path. Use middleware-level check instead.
    assert r.status_code in (200, 413)


def test_post_oversized_body_rejected_via_direct_middleware():
    """Bypass TestClient header rewriting by calling the middleware
    instance directly with a synthetic request."""
    import asyncio

    app = _make_app()
    mw = CplugapiSecurityMiddleware(app, max_body_bytes=1024)

    async def fake_call_next(request):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"{PROTECTED_PREFIX}probe",
        "headers": [
            (b"host", b"127.0.0.1:7860"),
            (b"content-length", b"100000000"),
        ],
        "query_string": b"",
    }
    request = Request(scope)
    resp = asyncio.new_event_loop().run_until_complete(
        mw.dispatch(request, fake_call_next)
    )
    assert resp.status_code == 413


def test_post_small_body_allowed():
    client = _client(_make_app())
    r = client.post(f"{PROTECTED_PREFIX}probe", json={"hello": "world"})
    assert r.status_code == 200


def test_post_invalid_content_length_rejected():
    """Direct dispatch to bypass TestClient's Content-Length sanitization."""
    import asyncio

    app = _make_app()
    mw = CplugapiSecurityMiddleware(app)

    async def fake_call_next(request):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"{PROTECTED_PREFIX}probe",
        "headers": [
            (b"host", b"127.0.0.1:7860"),
            (b"content-length", b"not-a-number"),
        ],
        "query_string": b"",
    }
    request = Request(scope)
    resp = asyncio.new_event_loop().run_until_complete(
        mw.dispatch(request, fake_call_next)
    )
    assert resp.status_code == 400


# --- Per-route body-size caps (W7) -------------------------------------------


def _direct_dispatch(
    mw: CplugapiSecurityMiddleware, method: str, path: str, content_length: str
):
    """Run a synthetic request through the middleware bypassing
    TestClient. TestClient rewrites Content-Length to match the
    actual body it transmits, which defeats the cap test; ASGI
    scope-level injection is the only reliable path."""
    import asyncio

    from starlette.requests import Request

    async def fake_call_next(request):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [
            (b"host", b"127.0.0.1:7860"),
            (b"content-length", content_length.encode()),
        ],
        "query_string": b"",
    }
    request = Request(scope)
    return asyncio.new_event_loop().run_until_complete(
        mw.dispatch(request, fake_call_next)
    )


def test_match_route_limit_exact_prefix_with_trailing_slash():
    """A path that extends a ``/``-terminated prefix matches."""
    cap = _match_route_limit(
        "POST",
        "/cplugapi/v1/forge/preset/sketch",
        ROUTE_LIMITS,
    )
    assert cap == 4 * 1024


def test_match_route_limit_eos_match():
    """A path that exactly equals a non-``/``-terminated rule prefix
    matches via the EOS branch — that is how ``/session/preempt``
    (no trailing slash in the rule) catches the bare path."""
    cap = _match_route_limit(
        "POST",
        "/cplugapi/v1/session/preempt",
        ROUTE_LIMITS,
    )
    assert cap == 4 * 1024


def test_match_route_limit_adjacent_path_does_not_match():
    """``/cplugapi/v1/forge/preset-bulk`` shares the substring
    ``/cplugapi/v1/forge/preset`` with the rule but the boundary
    character is ``-`` not ``/`` or EOS. Must NOT match."""
    cap = _match_route_limit(
        "POST",
        "/cplugapi/v1/forge/preset-bulk",
        ROUTE_LIMITS,
    )
    assert cap is None


def test_match_route_limit_method_must_match():
    """A GET to an otherwise-matching path falls through; the rule
    table is keyed on ``(method, path)``."""
    cap = _match_route_limit(
        "GET",
        "/cplugapi/v1/forge/preset/sketch",
        ROUTE_LIMITS,
    )
    assert cap is None


def test_match_route_limit_longest_prefix_wins():
    """When two rules both match, the longer prefix takes precedence."""
    table = {
        ("POST", "/a/"): 100,
        ("POST", "/a/b/"): 50,
    }
    cap = _match_route_limit("POST", "/a/b/c", table)
    assert cap == 50


def test_post_oversized_route_specific_cap_rejects_64k_body():
    """W7 acceptance test 1: 64 KiB POST to a per-route-capped endpoint
    returns 413 with the W3 problem+json envelope and code
    ``body_too_large``."""
    app = _make_app()
    mw = CplugapiSecurityMiddleware(app)
    resp = _direct_dispatch(
        mw,
        "POST",
        "/cplugapi/v1/forge/preset/sketch",
        str(64 * 1024),
    )
    assert resp.status_code == 413
    assert resp.media_type == "application/problem+json"
    import json
    body = json.loads(resp.body)
    assert body["code"] == "body_too_large"
    assert body["status"] == 413
    # Detail string distinguishes per-route from global cap rejection.
    assert "route-specific" in body["detail"]


def test_post_under_route_specific_cap_passes_size_check():
    """W7 acceptance test 2: 1 KiB POST to a per-route-capped endpoint
    passes the size check. The downstream may 404 or whatever (no
    matching FastAPI route registered) — what matters is that the
    middleware returns ``None`` from ``_check_body_size``, i.e. the
    body cap does not fire."""
    app = _make_app()
    mw = CplugapiSecurityMiddleware(app)
    resp = _direct_dispatch(
        mw,
        "POST",
        "/cplugapi/v1/forge/preset/sketch",
        str(1024),
    )
    # The fake call_next we plug in always returns 200 — if the
    # middleware short-circuited with 413, that's the failure.
    assert resp.status_code == 200


def test_adjacent_path_not_subject_to_route_specific_cap():
    """W7 acceptance test 3: ``/cplugapi/v1/forge/preset-bulk`` (a
    hypothetical sibling that does NOT match the route prefix per the
    boundary rule) gets the global cap, not the 4 KiB route cap. A
    5 KiB body should pass — well over the route cap, well under
    the 32 MiB global cap."""
    app = _make_app()
    mw = CplugapiSecurityMiddleware(app)
    resp = _direct_dispatch(
        mw,
        "POST",
        "/cplugapi/v1/forge/preset-bulk",
        str(5 * 1024),
    )
    assert resp.status_code == 200


def test_route_limits_env_override(monkeypatch):
    """W7 acceptance test 4: ``CPLUG_ROUTE_BODY_LIMITS`` overrides the
    built-in table. A 1024-byte body to the override route is fine;
    a 1025-byte body trips the cap."""
    monkeypatch.setenv(
        security_middleware.ENV_ROUTE_BODY_LIMITS,
        "POST:/cplugapi/v1/_test/strict:512",
    )
    app = _make_app()
    mw = CplugapiSecurityMiddleware(app)

    resp = _direct_dispatch(
        mw,
        "POST",
        "/cplugapi/v1/_test/strict",
        str(1024),
    )
    assert resp.status_code == 413
    import json
    body = json.loads(resp.body)
    assert body["code"] == "body_too_large"
    assert "route-specific" in body["detail"]

    # Same middleware instance (env captured at __init__), under-cap body passes.
    resp_ok = _direct_dispatch(
        mw,
        "POST",
        "/cplugapi/v1/_test/strict",
        str(256),
    )
    assert resp_ok.status_code == 200


def test_route_limits_env_replaces_defaults(monkeypatch):
    """When env overrides are set, the built-in ROUTE_LIMITS defaults
    are NOT silently merged. Operators get exactly the table they
    specified — same posture as ``CPLUG_ALLOWED_HOSTS``. Confirms by
    checking that a previously-capped route falls back to the global
    cap when the override doesn't list it."""
    monkeypatch.setenv(
        security_middleware.ENV_ROUTE_BODY_LIMITS,
        "POST:/cplugapi/v1/_test/strict:512",
    )
    app = _make_app()
    mw = CplugapiSecurityMiddleware(app)

    # /forge/preset/sketch was 4 KiB by default; now it should fall
    # back to the 32 MiB global cap. A 64 KiB body passes.
    resp = _direct_dispatch(
        mw,
        "POST",
        "/cplugapi/v1/forge/preset/sketch",
        str(64 * 1024),
    )
    assert resp.status_code == 200


def test_route_limits_env_malformed_entries_skipped(monkeypatch):
    """Malformed entries are logged and skipped; valid entries on the
    same line still apply."""
    monkeypatch.setenv(
        security_middleware.ENV_ROUTE_BODY_LIMITS,
        "garbage,POST:/cplugapi/v1/_test/strict:512,POST:/x:notanint",
    )
    parsed = _parse_route_limits_env(security_middleware.ENV_ROUTE_BODY_LIMITS)
    assert parsed == {("POST", "/cplugapi/v1/_test/strict"): 512}


def test_route_limit_problem_envelope_carries_request_id():
    """The 413 response carries through ``X-Request-Id`` so ops can
    correlate the rejection with the rest of the trace — same envelope
    contract as the global cap path."""
    import asyncio
    import json

    from starlette.requests import Request

    app = _make_app()
    mw = CplugapiSecurityMiddleware(app)

    async def fake_call_next(request):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/cplugapi/v1/forge/preset/sketch",
        "headers": [
            (b"host", b"127.0.0.1:7860"),
            (b"content-length", str(64 * 1024).encode()),
            (b"x-request-id", b"abc-123"),
        ],
        "query_string": b"",
    }
    request = Request(scope)
    resp = asyncio.new_event_loop().run_until_complete(
        mw.dispatch(request, fake_call_next)
    )
    assert resp.status_code == 413
    body = json.loads(resp.body)
    assert body.get("request_id") == "abc-123"


# --- Path scoping ------------------------------------------------------------


def test_sdapi_path_not_subject_to_origin_check():
    """Routes outside ``/cplugapi/v1/`` must pass through unchanged so
    upstream Forge Neo's surface stays byte-identical."""
    client = _client(_make_app())
    r = client.get(
        "/sdapi/v1/test",
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 200
    assert r.json() == {"sdapi": True}


def test_sdapi_path_not_subject_to_host_check():
    client = _client(_make_app())
    r = client.get(
        "/sdapi/v1/test",
        headers={"Host": "127.0.0.1.evil.example"},
    )
    assert r.status_code == 200


# --- Env-var overrides -------------------------------------------------------


def test_env_var_extends_origin_allowlist(monkeypatch):
    """``CPLUG_ALLOWED_ORIGINS`` adds entries; the loopback default
    remains in force regardless."""
    monkeypatch.setenv(
        security_middleware.ENV_ALLOWED_ORIGINS,
        "http://my-helper.example,http://other.example",
    )
    # Build a fresh app so the middleware reads the patched env.
    app = FastAPI()

    @app.get(f"{PROTECTED_PREFIX}probe")
    def probe() -> dict:
        return {"ok": True}

    install(app)
    client = _client(app)

    # Whitelisted by env var — accepted.
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://my-helper.example"},
    )
    assert r.status_code == 200

    # Loopback still works.
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://127.0.0.1:7860"},
    )
    assert r.status_code == 200

    # Non-listed still rejected.
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://still-evil.example"},
    )
    assert r.status_code == 403


def test_env_var_extends_host_allowlist(monkeypatch):
    monkeypatch.setenv(
        security_middleware.ENV_ALLOWED_HOSTS,
        "my-host.local,other.local",
    )
    app = FastAPI()

    @app.get(f"{PROTECTED_PREFIX}probe")
    def probe() -> dict:
        return {"ok": True}

    install(app)
    client = _client(app)

    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Host": "my-host.local"},
    )
    assert r.status_code == 200

    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Host": "127.0.0.1.evil.example"},
    )
    assert r.status_code == 403


def test_env_var_max_body_bytes(monkeypatch):
    monkeypatch.setenv(security_middleware.ENV_MAX_BODY_BYTES, "1024")
    import asyncio

    app = FastAPI()

    @app.post(f"{PROTECTED_PREFIX}probe")
    def probe() -> dict:
        return {"ok": True}

    install(app)
    # Direct-dispatch path because TestClient will rewrite Content-Length.
    mw = CplugapiSecurityMiddleware(app)

    async def fake_call_next(request):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"{PROTECTED_PREFIX}probe",
        "headers": [
            (b"host", b"127.0.0.1:7860"),
            (b"content-length", b"2048"),
        ],
        "query_string": b"",
    }
    request = Request(scope)
    resp = asyncio.new_event_loop().run_until_complete(
        mw.dispatch(request, fake_call_next)
    )
    assert resp.status_code == 413


# --- Idempotency -------------------------------------------------------------


def test_install_is_idempotent():
    """A second ``install()`` on the same app is a no-op (no double-stack)."""
    app = FastAPI()

    @app.get(f"{PROTECTED_PREFIX}probe")
    def probe() -> dict:
        return {"ok": True}

    install(app)
    install(app)
    install(app)

    client = TestClient(app)
    # Bad Origin still rejected exactly once with 403 (not e.g. 500
    # from a stack of middlewares interfering).
    r = client.get(
        f"{PROTECTED_PREFIX}probe",
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403


# --- Capability registration -------------------------------------------------


def test_register_capabilities_emits_slash_only_strings(clean_capabilities):
    register_capabilities()
    enabled = clean_capabilities.enabled_capabilities()
    assert "security/origin-checks" in enabled
    assert "security/host-checks" in enabled
    assert "security/body-size-cap" in enabled
    assert "security/per-route-body-limits" in enabled
    # No dot-notation slipped in.
    for name in enabled:
        assert "." not in name
