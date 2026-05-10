"""Tests for ``modules.cplugapi.ws_auth`` — WebSocket auth invariant shim.

The shim engages on every WebSocket scope under ``/cplugapi/v1/*``,
even though no production WS endpoints exist today. The forward
check works by exercising the shim's ASGI path directly with mock
``receive``/``send`` coroutines so the policy is verified without
depending on a working TestClient WebSocket transport (which has
proven flaky across starlette/httpx version combos).

If T31 (or any other contributor) later attaches a WS route under
``/cplugapi/v1/*`` without auth, the contract verified here covers
the policy regardless of how the route was attached — the shim sits
outermost in the middleware stack and screens every WS upgrade.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPBasicCredentials

from modules.cplugapi import setup_cplugapi, ws_auth
from modules.cplugapi.errors import CODES, PROBLEM_JSON


def _basic_b64(user: str, password: str) -> bytes:
    raw = f"{user}:{password}".encode("utf-8")
    return b"Basic " + base64.b64encode(raw)


class _RecordingSend:
    """Captures ASGI send messages so the test can assert what the
    shim emitted (status, headers, body)."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class _ConnectThenDisconnectReceive:
    """Mock ASGI receive coroutine. Yields ``websocket.connect`` once
    then ``websocket.disconnect``."""

    def __init__(self) -> None:
        self._stage = 0

    async def __call__(self) -> dict[str, Any]:
        self._stage += 1
        if self._stage == 1:
            return {"type": "websocket.connect"}
        return {"type": "websocket.disconnect", "code": 1000}


def _ws_scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "websocket",
        "path": path,
        "headers": headers or [],
    }


def _http_scope(path: str) -> dict[str, Any]:
    return {"type": "http", "path": path, "headers": []}


def _run(coro):
    """Drive an async test from a sync test function. Each test gets
    its own loop to avoid cross-test contamination."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Pass-through paths — the shim is a no-op and the inner app is reached.
# ---------------------------------------------------------------------------


def test_http_scope_falls_through_unchanged(clean_capabilities):
    """The shim must never touch HTTP traffic."""
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope["path"])

    shim = ws_auth.CplugapiWsAuthShim(inner, auth_dependency=lambda c: None)
    _run(shim(_http_scope("/cplugapi/v1/health"), _ConnectThenDisconnectReceive(), _RecordingSend()))
    assert inner_called == ["/cplugapi/v1/health"]


def test_ws_outside_cplugapi_prefix_falls_through(clean_capabilities):
    """A WS upgrade outside ``/cplugapi/v1/`` must pass through —
    invariant 1 (``/sdapi/v1/*`` byte-identity)."""
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope["path"])

    def reject_all(creds):
        raise HTTPException(status_code=401, detail="should not be called")

    shim = ws_auth.CplugapiWsAuthShim(inner, auth_dependency=reject_all)
    send = _RecordingSend()
    _run(shim(_ws_scope("/sdapi/v1/_test/ws"), _ConnectThenDisconnectReceive(), send))
    assert inner_called == ["/sdapi/v1/_test/ws"]
    # Crucially: shim sent NO rejection messages. Inner app got control.
    assert send.messages == []


def test_no_auth_configured_falls_through(clean_capabilities):
    """When ``--api-auth`` is not set (auth_dependency=None), the shim
    is a no-op even for cplugapi WS paths."""
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope["path"])

    shim = ws_auth.CplugapiWsAuthShim(inner, auth_dependency=None)
    send = _RecordingSend()
    _run(shim(_ws_scope("/cplugapi/v1/_test/ws"), _ConnectThenDisconnectReceive(), send))
    assert inner_called == ["/cplugapi/v1/_test/ws"]
    assert send.messages == []


# ---------------------------------------------------------------------------
# Rejection paths — emit the ASGI websocket.http.response.* events.
# ---------------------------------------------------------------------------


def _assert_rejected_403(send: _RecordingSend, expected_code: str) -> None:
    """Verify the shim emitted a problem+json 403 close."""
    assert len(send.messages) == 2
    start = send.messages[0]
    body_msg = send.messages[1]
    assert start["type"] == "websocket.http.response.start"
    assert start["status"] == 403
    headers = dict((k.decode(), v.decode()) for k, v in start["headers"])
    assert headers["content-type"] == PROBLEM_JSON
    assert body_msg["type"] == "websocket.http.response.body"
    body = json.loads(body_msg["body"])
    assert body["status"] == 403
    assert body["code"] == expected_code


def test_missing_authorization_rejected_with_problem_json(clean_capabilities):
    """No Authorization header on a cplugapi WS upgrade -> 403."""
    inner_called = []

    async def inner(*a, **k):
        inner_called.append(True)

    def reject_all(creds):
        raise HTTPException(status_code=401, detail="should not reach here")

    shim = ws_auth.CplugapiWsAuthShim(inner, auth_dependency=reject_all)
    send = _RecordingSend()
    _run(shim(_ws_scope("/cplugapi/v1/_test/ws"), _ConnectThenDisconnectReceive(), send))
    assert inner_called == []  # rejected before reaching inner
    _assert_rejected_403(send, CODES.AUTH_REQUIRED)


def test_malformed_basic_header_rejected(clean_capabilities):
    """Authorization header that doesn't decode as Basic -> 403, and the
    auth_dependency is NOT called (we don't pass garbage to it)."""
    auth_called = []

    def auth_dep(creds):
        auth_called.append(creds)

    shim = ws_auth.CplugapiWsAuthShim(lambda *a, **k: None, auth_dependency=auth_dep)
    send = _RecordingSend()
    _run(
        shim(
            _ws_scope(
                "/cplugapi/v1/_test/ws",
                headers=[(b"authorization", b"Basic !!!not-base64!!!")],
            ),
            _ConnectThenDisconnectReceive(),
            send,
        )
    )
    assert auth_called == []
    _assert_rejected_403(send, CODES.AUTH_REQUIRED)


def test_non_basic_scheme_rejected(clean_capabilities):
    """``Authorization: Bearer ...`` -> 403; the shim doesn't accept
    bearer tokens (the underlying auth dep is HTTPBasic-shaped)."""
    auth_called = []

    def auth_dep(creds):
        auth_called.append(creds)

    shim = ws_auth.CplugapiWsAuthShim(lambda *a, **k: None, auth_dependency=auth_dep)
    send = _RecordingSend()
    _run(
        shim(
            _ws_scope(
                "/cplugapi/v1/_test/ws",
                headers=[(b"authorization", b"Bearer abc.def.ghi")],
            ),
            _ConnectThenDisconnectReceive(),
            send,
        )
    )
    assert auth_called == []
    _assert_rejected_403(send, CODES.AUTH_REQUIRED)


def test_invalid_basic_credentials_rejected(clean_capabilities):
    """Valid Basic header shape but auth_dependency rejects -> 403,
    code AUTH_FAILED (distinguishable from missing creds)."""
    auth_called = []

    def auth_dep(creds: HTTPBasicCredentials):
        auth_called.append((creds.username, creds.password))
        raise HTTPException(status_code=401, detail="bad creds")

    shim = ws_auth.CplugapiWsAuthShim(lambda *a, **k: None, auth_dependency=auth_dep)
    send = _RecordingSend()
    _run(
        shim(
            _ws_scope(
                "/cplugapi/v1/_test/ws",
                headers=[(b"authorization", _basic_b64("u", "wrong"))],
            ),
            _ConnectThenDisconnectReceive(),
            send,
        )
    )
    assert auth_called == [("u", "wrong")]  # auth dep WAS called
    _assert_rejected_403(send, CODES.AUTH_FAILED)


def test_valid_credentials_pass_to_inner_app(clean_capabilities):
    """Valid Basic creds, auth_dependency accepts -> inner app reached."""
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope["path"])

    def auth_dep(creds: HTTPBasicCredentials):
        if creds.username == "u" and creds.password == "correct":
            return creds
        raise HTTPException(status_code=401, detail="bad")

    shim = ws_auth.CplugapiWsAuthShim(inner, auth_dependency=auth_dep)
    send = _RecordingSend()
    _run(
        shim(
            _ws_scope(
                "/cplugapi/v1/_test/ws",
                headers=[(b"authorization", _basic_b64("u", "correct"))],
            ),
            _ConnectThenDisconnectReceive(),
            send,
        )
    )
    assert inner_called == ["/cplugapi/v1/_test/ws"]
    assert send.messages == []  # shim sent nothing; inner had full control


# ---------------------------------------------------------------------------
# Wire-up
# ---------------------------------------------------------------------------


def test_capability_registered(clean_capabilities):
    from modules.cplugapi import capabilities

    ws_auth.register_capabilities()
    assert "security/ws-auth-enforced" in capabilities.enabled_capabilities()


def test_install_is_idempotent(clean_capabilities):
    app = FastAPI()
    setup_cplugapi(app)
    n1 = len(app.user_middleware)
    ws_auth.install(app)
    n2 = len(app.user_middleware)
    assert n1 == n2


def test_install_via_setup_cplugapi_threads_auth_dependency(clean_capabilities):
    """End-to-end wiring check: setup_cplugapi installs ws_auth with
    AN auth_dependency. Note: as of W8, ``rate_limit.observe_auth_failures``
    wraps the inner auth dep before it reaches ws_auth.install, so the
    callable identity is the wrap, not the user-supplied original. We
    only assert that *something* callable is wired."""

    def auth_dep(creds):
        return creds

    app = FastAPI()
    setup_cplugapi(app, auth_dependency=auth_dep)
    for m in app.user_middleware:
        if m.cls is ws_auth.CplugapiWsAuthShim:
            wired = m.kwargs.get("auth_dependency")
            assert wired is not None
            assert callable(wired)
            break
    else:
        pytest.fail("ws_auth middleware not installed by setup_cplugapi")
