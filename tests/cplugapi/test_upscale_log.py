"""Tests for ``modules.cplugapi.upscale_log``.

The middleware is pure ASGI and tags two upscale flows:
- ``POST /sdapi/v1/extra-single-image`` (deterministic — different endpoint)
- ``POST /sdapi/v1/img2img`` carrying ``X-Cplug-Intent: upscale``-family header

Tests exercise both happy paths plus the silent-passthrough cases.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import setup_cplugapi


@pytest.fixture
def caplog_upscale(caplog):
    """Forge's ``setup_logger`` flips ``propagate=False`` on our cplugapi
    loggers; restore propagation for the test so caplog (rooted at root
    logger) captures the line."""
    logger = logging.getLogger("cplugapi.upscale")
    original = logger.propagate
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="cplugapi.upscale")
    try:
        yield caplog
    finally:
        logger.propagate = original


def _records(caplog):
    return [r for r in caplog.records if r.name == "cplugapi.upscale"]


def _build_app():
    """Build a fresh app with cplugapi mounted plus stand-in /sdapi
    routes — Forge's real surface isn't importable in the test env."""
    app = FastAPI()
    setup_cplugapi(app)

    @app.post("/sdapi/v1/extra-single-image")
    def extras(payload: dict):
        return {"image": "...", "html_info": ""}

    @app.post("/sdapi/v1/img2img")
    def img2img(payload: dict):
        return {"images": ["..."]}

    @app.post("/sdapi/v1/txt2img")
    def txt2img(payload: dict):
        return {"images": ["..."]}

    return app


def test_logs_extras_upscale(caplog_upscale, progress_stub, clean_capabilities):
    """Every POST to /sdapi/v1/extra-single-image must produce one line
    tagged ``type=extras`` — that's the deterministic case."""
    client = TestClient(_build_app())
    r = client.post("/sdapi/v1/extra-single-image", json={"image": "..."})
    assert r.status_code == 200
    records = _records(caplog_upscale)
    assert len(records) == 1
    assert records[0].upscale_type == "extras"
    assert "extra-single-image" in records[0].path


def test_logs_img2img_upscale_with_header(
    caplog_upscale, progress_stub, clean_capabilities,
):
    """img2img with ``X-Cplug-Intent: upscale`` is tagged
    ``type=img2img-refine`` — distinguishes the upscale flow from
    ordinary sketch-driven img2img on the same endpoint."""
    client = TestClient(_build_app())
    r = client.post(
        "/sdapi/v1/img2img",
        json={"init_images": ["..."]},
        headers={"X-Cplug-Intent": "upscale"},
    )
    assert r.status_code == 200
    records = _records(caplog_upscale)
    assert len(records) == 1
    assert records[0].upscale_type == "img2img-refine"


def test_accepts_alternate_intent_values(
    caplog_upscale, progress_stub, clean_capabilities,
):
    """The intent header accepts a few variant spellings so the client
    doesn't have to remember the exact one."""
    client = TestClient(_build_app())
    for value in ("upscale-img2img", "upscale-refine", "UPSCALE"):
        r = client.post(
            "/sdapi/v1/img2img",
            json={"init_images": ["..."]},
            headers={"X-Cplug-Intent": value},
        )
        assert r.status_code == 200
    records = _records(caplog_upscale)
    assert len(records) == 3


def test_silent_for_img2img_without_header(
    caplog_upscale, progress_stub, clean_capabilities,
):
    """Ordinary sketch-driven img2img (no intent header) MUST NOT log.
    Otherwise every gen during a live-sketching session would trigger
    a misleading 'upscale request' line."""
    client = TestClient(_build_app())
    r = client.post("/sdapi/v1/img2img", json={"init_images": ["..."]})
    assert r.status_code == 200
    assert _records(caplog_upscale) == []


def test_silent_for_txt2img(caplog_upscale, progress_stub, clean_capabilities):
    """txt2img is never an upscale flow — no line regardless of header."""
    client = TestClient(_build_app())
    r = client.post(
        "/sdapi/v1/txt2img",
        json={"prompt": "test"},
        headers={"X-Cplug-Intent": "upscale"},  # ignored on txt2img
    )
    assert r.status_code == 200
    assert _records(caplog_upscale) == []


def test_silent_outside_sdapi(caplog_upscale, progress_stub, clean_capabilities):
    """Paths outside the matched set are pass-through with zero log
    overhead. Exercises the early-return path for non-upscale traffic."""
    app = _build_app()

    @app.get("/some-other")
    def other():
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/some-other")
    assert r.status_code == 200
    assert _records(caplog_upscale) == []


def test_kill_switch_disables_emission(
    monkeypatch, caplog_upscale, progress_stub, clean_capabilities,
):
    """``CPLUG_UPSCALE_LOG=0`` keeps the middleware silent. Read-once-at-
    install means the env var has to be set BEFORE setup_cplugapi runs."""
    monkeypatch.setenv("CPLUG_UPSCALE_LOG", "0")
    client = TestClient(_build_app())
    r = client.post("/sdapi/v1/extra-single-image", json={"image": "..."})
    assert r.status_code == 200
    assert _records(caplog_upscale) == []


def test_kill_switch_omits_capability(
    monkeypatch, progress_stub, clean_capabilities,
):
    """When emission is disabled, ``upscale-log`` must be absent from
    /health.capabilities[] so a client can detect the off state."""
    monkeypatch.setenv("CPLUG_UPSCALE_LOG", "0")
    from modules.cplugapi import PREFIX
    client = TestClient(_build_app())
    caps = client.get(f"{PREFIX}/health").json()["capabilities"]
    assert "upscale-log" not in caps


def test_capability_registered_when_enabled(progress_stub, clean_capabilities):
    """``upscale-log`` advertises the tagged-log feature so clients can
    feature-detect it before relying on the line for their own
    diagnostics."""
    from modules.cplugapi import PREFIX
    client = TestClient(_build_app())
    caps = client.get(f"{PREFIX}/health").json()["capabilities"]
    assert "upscale-log" in caps


def test_request_size_captured(
    caplog_upscale, progress_stub, clean_capabilities,
):
    """Content-Length surfaces as ``in`` so an operator can spot
    abnormally-large upscale payloads (4× SDXL outputs reach tens of
    MB) without parsing the body."""
    client = TestClient(_build_app())
    r = client.post(
        "/sdapi/v1/extra-single-image",
        json={"image": "x" * 1024},  # not realistic content but real Content-Length
    )
    assert r.status_code == 200
    records = _records(caplog_upscale)
    assert len(records) == 1
    assert records[0].in_bytes > 0
