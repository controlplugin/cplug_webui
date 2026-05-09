"""Tests for ``modules.cplugapi.sdapi_observer``.

The observer is pure-ASGI (not BaseHTTPMiddleware) so it can sit on
``/sdapi/v1/*`` without triggering the streaming-response wrapper bug
documented in encode/starlette#1438. Tests exercise:

- One log line per matching request, with method/path/status/dur_ms.
- Pure passthrough on paths outside the configured prefixes.
- Streaming endpoints under ``/sdapi/v1/*`` still work cleanly (the
  observer must not buffer or break the response stream).
- Exceptions surface to the caller AND the line still emits with an
  ``error`` field.
- Capability registered.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from modules.cplugapi import setup_cplugapi


@pytest.fixture
def caplog_sdapi(caplog):
    """Forge's ``setup_logger`` flips ``propagate=False`` on our
    cplugapi loggers so the messages bypass the root handler and get
    rendered through Rich. Pytest's ``caplog`` attaches to root, so
    we need to temporarily restore propagation for the duration of
    the test to capture lines."""
    logger = logging.getLogger("cplugapi.sdapi")
    original = logger.propagate
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="cplugapi.sdapi")
    try:
        yield caplog
    finally:
        logger.propagate = original


def _records(caplog):
    return [r for r in caplog.records if r.name == "cplugapi.sdapi"]


def _build_app_with_sdapi_route():
    """Construct a fresh app with cplugapi mounted plus a stand-in
    /sdapi/v1/foo route — Forge's real /sdapi/v1/* surface isn't
    importable in the test env without booting half the WebUI."""
    app = FastAPI()
    setup_cplugapi(app)

    @app.get("/sdapi/v1/foo")
    def sdapi_foo():
        return {"ok": True}

    @app.get("/some-other-path")
    def other():
        return {"ok": True}

    @app.get("/sdapi/v1/streamed")
    def sdapi_stream():
        async def gen():
            for i in range(3):
                yield f"part{i}".encode()
        return StreamingResponse(gen())

    return app


def test_logs_sdapi_request(caplog_sdapi, progress_stub, clean_capabilities):
    client = TestClient(_build_app_with_sdapi_route())
    r = client.get("/sdapi/v1/foo")
    assert r.status_code == 200
    records = _records(caplog_sdapi)
    assert len(records) == 1
    assert records[0].method == "GET"
    assert records[0].path == "/sdapi/v1/foo"
    assert records[0].status == 200
    assert records[0].dur_ms >= 0


def test_silent_outside_prefix(caplog_sdapi, progress_stub, clean_capabilities):
    """Paths outside /sdapi/v1/* must not emit. Otherwise every Gradio
    long-poll heartbeat would flood the log."""
    client = TestClient(_build_app_with_sdapi_route())
    r = client.get("/some-other-path")
    assert r.status_code == 200
    assert _records(caplog_sdapi) == []


def test_streaming_response_still_works(caplog_sdapi, progress_stub, clean_capabilities):
    """The observer must not buffer streaming responses. The body must
    arrive intact and the line must still emit on completion."""
    client = TestClient(_build_app_with_sdapi_route())
    with client.stream("GET", "/sdapi/v1/streamed") as r:
        body = b"".join(r.iter_bytes())
    assert body == b"part0part1part2"
    records = _records(caplog_sdapi)
    assert len(records) == 1
    assert records[0].status == 200


def test_exception_in_handler_logs_with_error_field(caplog_sdapi, progress_stub, clean_capabilities):
    """Failed gens must still surface in the observer log so an operator
    diagnosing client-reported failures can see when something blew up
    on /sdapi/v1/*."""
    app = FastAPI()
    setup_cplugapi(app)

    class _Boom(RuntimeError):
        pass

    @app.get("/sdapi/v1/explode")
    def explode():
        raise _Boom("simulated")

    client = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(_Boom):
        client.get("/sdapi/v1/explode")
    records = _records(caplog_sdapi)
    assert len(records) == 1
    assert getattr(records[0], "error", None) == "_Boom"


def test_request_size_captured(caplog_sdapi, progress_stub, clean_capabilities):
    """Content-Length on POST is logged so user can see request size
    without parsing the body (which would be expensive for img2img)."""
    app = FastAPI()
    setup_cplugapi(app)

    @app.post("/sdapi/v1/echo")
    def echo(data: dict):
        return data

    client = TestClient(app)
    payload = {"steps": 4, "width": 1024, "height": 1024}
    r = client.post("/sdapi/v1/echo", json=payload)
    assert r.status_code == 200
    records = _records(caplog_sdapi)
    assert len(records) == 1
    assert records[0].in_bytes > 0  # Content-Length was set


def test_capability_registered(progress_stub, clean_capabilities):
    """``sdapi-request-log`` must appear in /health.capabilities."""
    from modules.cplugapi import PREFIX
    app = FastAPI()
    setup_cplugapi(app)
    client = TestClient(app)
    caps = client.get(f"{PREFIX}/health").json()["capabilities"]
    assert "sdapi-request-log" in caps
