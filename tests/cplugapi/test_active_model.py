"""Tests for ``GET /cplugapi/v1/models/active``."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, setup_cplugapi


def _make_client(auth_dependency=None):
    app = FastAPI()
    setup_cplugapi(app, auth_dependency=auth_dependency)
    return TestClient(app)


def _install_sd_models_stub(model_obj):
    """Stub modules.sd_models so the endpoint sees model_obj as loaded."""
    stub = types.ModuleType("modules.sd_models")

    class _Holder:
        def __init__(self, m):
            self._m = m

        def get_sd_model(self):
            return self._m

    stub.model_data = _Holder(model_obj)
    stub.checkpoints_list = {}  # not used by this endpoint, but present
    sys.modules["modules.sd_models"] = stub


def test_active_unloaded(clean_capabilities):
    class FakeInitialModel:
        pass
    _install_sd_models_stub(FakeInitialModel())

    r = _make_client().get(f"{PREFIX}/models/active")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "loaded": False,
        "arch": None,
        "engine_class": None,
        "checkpoint": None,
        "checkpoint_hash": None,
        "checkpoint_sha256": None,
    }


def test_active_sdxl_loaded(clean_capabilities):
    class StableDiffusionXL:
        pass
    engine = StableDiffusionXL()
    engine.sd_checkpoint_info = SimpleNamespace(
        name="Illustrious-XL-v2.0.safetensors",
        shorthash="abcd123456",
        sha256="d" * 64,
    )
    _install_sd_models_stub(engine)

    r = _make_client().get(f"{PREFIX}/models/active")
    assert r.status_code == 200
    body = r.json()
    assert body["loaded"] is True
    assert body["arch"] == "sdxl"
    assert body["engine_class"] == "StableDiffusionXL"
    assert body["checkpoint"] == "Illustrious-XL-v2.0.safetensors"
    assert body["checkpoint_hash"] == "abcd123456"


def test_active_unknown_engine_class(clean_capabilities):
    """A future Forge engine we don't know about returns arch=unknown."""
    class FutureEngine:
        pass
    engine = FutureEngine()
    engine.sd_checkpoint_info = SimpleNamespace(name="x.safetensors", shorthash="0", sha256="0")
    _install_sd_models_stub(engine)

    r = _make_client().get(f"{PREFIX}/models/active")
    assert r.status_code == 200
    body = r.json()
    assert body["arch"] == "unknown"
    assert body["engine_class"] == "FutureEngine"


def test_active_checkpoint_basename_only(clean_capabilities):
    """Plan privacy invariant: never surface absolute paths."""
    class StableDiffusion:
        pass
    engine = StableDiffusion()
    engine.sd_checkpoint_info = SimpleNamespace(
        name="subfolder/nested/v1-5-pruned.safetensors",
        shorthash="aaaaaaa",
        sha256="b" * 64,
    )
    _install_sd_models_stub(engine)

    r = _make_client().get(f"{PREFIX}/models/active")
    assert r.json()["checkpoint"] == "v1-5-pruned.safetensors"


def test_active_auth_required_returns_401(clean_capabilities):
    def reject_all():
        raise HTTPException(status_code=401, detail="nope")

    class FakeInitialModel:
        pass
    _install_sd_models_stub(FakeInitialModel())

    client = _make_client(auth_dependency=reject_all)
    r = client.get(f"{PREFIX}/models/active")
    assert r.status_code == 401


def test_active_auth_disabled_returns_200(clean_capabilities):
    """Optional-auth posture: no auth required when --api-auth unset."""
    class FakeInitialModel:
        pass
    _install_sd_models_stub(FakeInitialModel())

    r = _make_client(auth_dependency=None).get(f"{PREFIX}/models/active")
    assert r.status_code == 200
