"""Tests for ``modules.cplugapi.livez_readyz`` endpoints."""

from __future__ import annotations

import sys

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, livez_readyz, setup_cplugapi


def _make_client_with(extra_attach):
    """Mount cplugapi + livez_readyz routes."""
    app = FastAPI()
    setup_cplugapi(app)
    extra = APIRouter()
    extra_attach(extra)
    app.include_router(extra, prefix=PREFIX)
    return TestClient(app)


def setup_function(_):
    """Reset shared state between tests."""
    livez_readyz.clear_last_error()


def test_livez_always_returns_200(clean_capabilities):
    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/livez")
    assert r.status_code == 200
    assert r.json() == {"status": "live"}


def test_livez_does_not_check_models(clean_capabilities):
    """Even when shared.opts is missing the model + last_error is
    recorded, livez still returns 200."""
    livez_readyz.record_last_error("oom", "out of memory")
    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/livez")
    assert r.status_code == 200


def test_readyz_returns_200_when_torch_and_opts_present(clean_capabilities, monkeypatch):
    """Happy path: torch importable + sd_model_checkpoint set + no err."""
    # Patch shared.opts to report a loaded model.
    import modules.shared as shared

    class _FakeOpts:
        def __init__(self, data: dict) -> None:
            self.data = data

    fake_opts = _FakeOpts({"sd_model_checkpoint": "model.safetensors"})
    monkeypatch.setattr(shared, "opts", fake_opts, raising=False)

    # Stub a minimal torch in sys.modules so the importable check passes
    # even on machines without torch installed.
    if "torch" not in sys.modules:
        import types

        torch_stub = types.ModuleType("torch")
        sys.modules["torch"] = torch_stub
        added_stub = True
    else:
        added_stub = False

    try:
        client = _make_client_with(livez_readyz.attach)
        r = client.get(f"{PREFIX}/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["checks"]["torch_importable"] is True
        assert body["checks"]["model_loaded"] is True
        assert body["checks"]["last_error"] is None
    finally:
        if added_stub:
            sys.modules.pop("torch", None)


def test_readyz_503_when_last_error_recorded(clean_capabilities):
    livez_readyz.record_last_error("oom", "GPU ran out of memory")

    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    err = body["checks"]["last_error"]
    assert err["kind"] == "oom"
    assert err["detail"] == "GPU ran out of memory"
    assert "recorded_at" in err


def test_readyz_503_when_model_explicitly_unloaded(clean_capabilities, monkeypatch):
    """sd_model_checkpoint == '' -> hard not-ready."""
    import modules.shared as shared

    class _FakeOpts:
        def __init__(self, data: dict) -> None:
            self.data = data

    monkeypatch.setattr(shared, "opts", _FakeOpts({"sd_model_checkpoint": ""}), raising=False)

    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["model_loaded"] is False


def test_readyz_treats_unknown_model_state_as_ready(clean_capabilities, monkeypatch):
    """When ``shared.opts`` is missing entirely (unit-test fixture state),
    we cannot distinguish "no model" from "haven't probed yet". Per spec,
    treat unknown -> ready so a half-booted webui is not stuck red."""
    import modules.shared as shared

    # Drop opts attribute if present.
    monkeypatch.delattr(shared, "opts", raising=False)

    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/readyz")
    # Whether torch is genuinely importable here drives the result. If
    # the test env imports torch successfully, we're ready. Otherwise
    # the response is still 503 — but model_loaded should be ``null``.
    body = r.json()
    assert body["checks"]["model_loaded"] is None


def test_record_then_clear_round_trip(clean_capabilities):
    assert livez_readyz.get_last_error() is None
    livez_readyz.record_last_error("checkpoint_load", "missing file")
    err = livez_readyz.get_last_error()
    assert err is not None
    assert err["kind"] == "checkpoint_load"
    assert err["detail"] == "missing file"

    livez_readyz.clear_last_error()
    assert livez_readyz.get_last_error() is None


def test_get_last_error_returns_a_copy(clean_capabilities):
    """Mutating the returned dict must not poison the registry."""
    livez_readyz.record_last_error("k", "v")
    snap = livez_readyz.get_last_error()
    snap["kind"] = "MUTATED"
    again = livez_readyz.get_last_error()
    assert again["kind"] == "k"


def test_register_capabilities_adds_livez_and_readyz(clean_capabilities):
    from modules.cplugapi import capabilities

    livez_readyz.register_capabilities()
    enabled = capabilities.enabled_capabilities()
    assert "livez" in enabled
    assert "readyz" in enabled


def test_readyz_503_when_torch_not_importable(clean_capabilities, monkeypatch):
    """Force the torch import inside the probe to fail."""
    monkeypatch.setattr(livez_readyz, "_torch_importable", lambda: False)
    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["torch_importable"] is False
