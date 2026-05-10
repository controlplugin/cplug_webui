"""Tests for ``modules.cplugapi.livez_readyz`` endpoints.

After W1, both ``/livez`` and ``/readyz`` are mounted on the public
router. The ``/readyz`` body is sanitised for unauthenticated probes
(booleans only); ``?verbose=1`` lifts the sanitisation but requires
Basic auth when ``--api-auth`` is configured.
"""

from __future__ import annotations

import base64
import sys

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, livez_readyz, setup_cplugapi


def _make_client_with(extra_attach):
    """Mount cplugapi + livez_readyz routes (legacy helper).

    Used by the older direct-attach tests that don't exercise the
    public/private split. New tests prefer ``_make_full_client`` which
    wires the route through ``setup_cplugapi`` so the public/auth
    posture matches production.
    """
    app = FastAPI()
    setup_cplugapi(app)
    extra = APIRouter()
    extra_attach(extra)
    app.include_router(extra, prefix=PREFIX)
    return TestClient(app)


def _make_full_client(auth_dependency=None):
    """Mount the full cplugapi surface — exercises the public/private
    split and verbose-mode auth wiring."""
    app = FastAPI()
    setup_cplugapi(app, auth_dependency=auth_dependency)
    return TestClient(app)


def _basic_header(user: str, password: str) -> dict[str, str]:
    raw = f"{user}:{password}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def setup_function(_):
    """Reset shared state between tests."""
    livez_readyz.clear_last_error()
    livez_readyz.clear_draining()


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
    import modules.shared as shared

    class _FakeOpts:
        def __init__(self, data: dict) -> None:
            self.data = data

    fake_opts = _FakeOpts({"sd_model_checkpoint": "model.safetensors"})
    monkeypatch.setattr(shared, "opts", fake_opts, raising=False)

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
        # Sanitised public body — has_error bool, not last_error dict.
        assert body["checks"]["has_error"] is False
        assert "last_error" not in body["checks"]
        assert body["checks"]["draining"] is False
    finally:
        if added_stub:
            sys.modules.pop("torch", None)


def test_readyz_503_when_last_error_recorded(clean_capabilities):
    """Sanitised public body reports has_error=true, no detail."""
    livez_readyz.record_last_error("oom", "GPU ran out of memory")

    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["has_error"] is True
    # Default body must NOT leak the error detail.
    assert "last_error" not in body["checks"]


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

    monkeypatch.delattr(shared, "opts", raising=False)

    client = _make_client_with(livez_readyz.attach)
    r = client.get(f"{PREFIX}/readyz")
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


# ---------------------------------------------------------------------------
# W1 — public-router posture + ?verbose=1 auth gate
# ---------------------------------------------------------------------------


def test_probes_work_unauth_when_api_auth_set(clean_capabilities):
    """Even with --api-auth configured, /livez and /readyz must work
    without credentials (k8s probe compatibility)."""

    def reject_all(creds):
        raise HTTPException(status_code=401, detail="nope")

    client = _make_full_client(auth_dependency=reject_all)
    assert client.get(f"{PREFIX}/livez").status_code == 200
    assert client.get(f"{PREFIX}/readyz").status_code in (200, 503)


def test_readyz_verbose_requires_auth_when_api_auth_set(clean_capabilities):
    """?verbose=1 must reject unauthenticated callers when --api-auth is on."""

    def reject_all(creds):
        raise HTTPException(status_code=401, detail="bad creds")

    client = _make_full_client(auth_dependency=reject_all)
    r = client.get(f"{PREFIX}/readyz?verbose=1")
    assert r.status_code == 401


def test_readyz_verbose_with_valid_creds(clean_capabilities):
    """?verbose=1 with valid credentials returns the diagnostic body."""

    def accept(creds: HTTPBasicCredentials):
        if creds.username == "u" and creds.password == "p":
            return creds
        raise HTTPException(status_code=401, detail="bad creds")

    livez_readyz.record_last_error("checkpoint_load", "missing fixture")
    client = _make_full_client(auth_dependency=accept)
    r = client.get(f"{PREFIX}/readyz?verbose=1", headers=_basic_header("u", "p"))
    assert r.status_code == 503
    body = r.json()
    # Verbose body includes full last_error record.
    err = body["checks"]["last_error"]
    assert err is not None
    assert err["kind"] == "checkpoint_load"
    assert err["detail"] == "missing fixture"
    assert "has_error" not in body["checks"]


def test_readyz_verbose_unrestricted_when_no_api_auth(clean_capabilities):
    """Without --api-auth configured (auth_dependency=None), verbose is
    allowed unrestricted — local-dev / desktop posture."""
    livez_readyz.record_last_error("k", "v")
    client = _make_full_client(auth_dependency=None)
    r = client.get(f"{PREFIX}/readyz?verbose=1")
    body = r.json()
    assert body["checks"]["last_error"] is not None
    assert body["checks"]["last_error"]["detail"] == "v"


def test_readyz_draining_flag_visible_unauth(clean_capabilities):
    """Operational drain state (W12) must be observable on the public
    body — k8s probes need to see ``draining: true`` to pull the pod
    from rotation. Drain is operational state, not a leak vector."""
    livez_readyz.set_draining(True)
    try:
        client = _make_full_client()
        r = client.get(f"{PREFIX}/readyz")
        assert r.status_code == 503
        body = r.json()
        assert body["checks"]["draining"] is True
    finally:
        livez_readyz.clear_draining()


def test_readyz_verbose_invalid_basic_header(clean_capabilities):
    """Garbage Authorization header on ?verbose=1 -> 401, not 500."""

    def accept(creds):
        return creds

    client = _make_full_client(auth_dependency=accept)
    r = client.get(
        f"{PREFIX}/readyz?verbose=1",
        headers={"Authorization": "Basic !!!not-base64!!!"},
    )
    assert r.status_code == 401
