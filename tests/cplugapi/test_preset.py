"""Endpoint tests for ``POST /cplugapi/v1/forge/preset/{name}``."""

from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, setup_cplugapi


@pytest.fixture
def opts_stub():
    """Install a tiny ``modules.shared.opts`` stub so the preset endpoint
    can flip values without booting the real webui options registry."""
    shared = sys.modules["modules.shared"]

    class _Opts:
        def __init__(self) -> None:
            self.data: dict = {}

        def set(self, name: str, value):
            self.data[name] = value

    opts = _Opts()
    shared.opts = opts
    yield opts
    if hasattr(shared, "opts"):
        delattr(shared, "opts")


def _make_client():
    app = FastAPI()
    setup_cplugapi(app)
    return TestClient(app)


def test_unknown_preset_returns_404(clean_capabilities, opts_stub):
    client = _make_client()
    r = client.post(f"{PREFIX}/forge/preset/nonexistent")
    assert r.status_code == 404
    assert "unknown preset" in r.json()["detail"]


def test_default_preset_resets_opts(clean_capabilities, opts_stub):
    opts_stub.data["show_progress_type"] = "TAESD"
    opts_stub.data["token_merging_ratio"] = 0.5
    client = _make_client()
    r = client.post(f"{PREFIX}/forge/preset/default")
    assert r.status_code == 200
    body = r.json()
    assert body["preset"] == "default"
    assert opts_stub.data["show_progress_type"] == "RGB"
    assert opts_stub.data["token_merging_ratio"] == 0.0
    assert opts_stub.data["show_progress_every_n_steps"] == 1


def test_sketch_preset_flips_live_preview_to_taesd(clean_capabilities, opts_stub):
    client = _make_client()
    r = client.post(f"{PREFIX}/forge/preset/sketch")
    assert r.status_code == 200
    body = r.json()
    assert body["preset"] == "sketch"
    assert opts_stub.data["show_progress_type"] == "TAESD"
    assert opts_stub.data["show_progress_every_n_steps"] == 5
    assert opts_stub.data["token_merging_ratio"] == 0.3
    assert opts_stub.data["token_merging_ratio_hr"] == 0.3
    # CUDA warmup field is reported even when no CUDA is present (False).
    assert "cuda_warmup" in body["applied"]


def test_sketch_preset_capability_advertised(clean_capabilities, opts_stub):
    client = _make_client()
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    assert "forge/preset" in r.json()["capabilities"]
