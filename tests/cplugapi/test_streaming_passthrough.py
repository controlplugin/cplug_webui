"""Regression test: cplugapi middlewares must not interact with
streaming responses on non-cplugapi paths.

Background
----------
All four cplugapi middlewares (access_log, request_id, security,
idempotency) inherit from Starlette's ``BaseHTTPMiddleware``. That
base class wraps the entire request through anyio task groups that
buffer the response via a memory-channel. When a downstream endpoint
returns a ``StreamingResponse`` and the underlying generator raises
mid-stream (e.g., Gradio long-poll endpoints firing on client
disconnect), the channel-based plumbing produces:

    RuntimeError: No response returned.

…masking the real error. This is documented at
https://github.com/encode/starlette/issues/1438.

Mitigation
----------
Each middleware overrides ``__call__`` to short-circuit BEFORE the
``BaseHTTPMiddleware`` wrapper runs, on paths outside
``/cplugapi/v1/*``. Pure passthrough — ``await self.app(scope,
receive, send)`` — leaves the response handling identical to
"middleware not installed".

This test reproduces the original failure mode (a streaming endpoint
whose generator raises) and asserts that the cplugapi middlewares
let the original exception through verbatim, instead of converting
it into the spurious ``RuntimeError: No response returned``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from modules.cplugapi import setup_cplugapi


class _StreamingBoom(RuntimeError):
    """Marker exception for the streaming-failure path. Distinguishes
    'real downstream error' from 'BaseHTTPMiddleware wrapper bug'."""


def _make_app_with_streaming_endpoint():
    app = FastAPI()
    setup_cplugapi(app)

    async def busted_generator():
        yield b"first chunk"
        raise _StreamingBoom("simulated streaming failure")

    @app.get("/non-cplugapi-stream")
    def stream():
        return StreamingResponse(busted_generator())

    return app


def test_streaming_failure_on_non_cplugapi_path_is_not_masked(progress_stub, clean_capabilities):
    """A streaming endpoint outside /cplugapi/v1/* must surface its
    real error, not the spurious 'No response returned' produced by
    BaseHTTPMiddleware buffering.

    With the __call__ short-circuit in place, the cplugapi middlewares
    pass the request through to the streaming endpoint without
    wrapping. The TestClient sees either the partial body followed by
    the exception, or the exception during iteration — but never
    'No response returned'."""
    app = _make_app_with_streaming_endpoint()
    client = TestClient(app, raise_server_exceptions=True)

    with pytest.raises(_StreamingBoom):
        # raise_server_exceptions=True surfaces the inner exception.
        # Pre-fix this would have raised RuntimeError("No response returned")
        # instead — that's the regression we're guarding against.
        with client.stream("GET", "/non-cplugapi-stream") as r:
            for _ in r.iter_bytes():
                pass


def test_streaming_endpoint_unaffected_when_path_outside_prefix(progress_stub, clean_capabilities):
    """A clean (non-raising) streaming endpoint outside the prefix
    must work exactly as if cplugapi were not installed at all.
    Validates the passthrough doesn't accidentally drop bytes."""
    app = FastAPI()
    setup_cplugapi(app)

    async def clean_generator():
        for i in range(3):
            yield f"chunk{i}".encode()

    @app.get("/non-cplugapi-stream-clean")
    def stream():
        return StreamingResponse(clean_generator())

    client = TestClient(app)
    with client.stream("GET", "/non-cplugapi-stream-clean") as r:
        body = b"".join(r.iter_bytes())
    assert body == b"chunk0chunk1chunk2"


def test_cplugapi_paths_still_route_through_middlewares(progress_stub, clean_capabilities):
    """Sanity: the __call__ short-circuit must NOT bypass middlewares
    for cplugapi requests. /health hitting the access_log + request_id
    chain still returns successfully and the X-Request-Id header is
    echoed (proving request_id middleware ran)."""
    from modules.cplugapi import PREFIX
    app = FastAPI()
    setup_cplugapi(app)
    client = TestClient(app)
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    assert r.headers.get("X-Request-Id", "").startswith("req_")
