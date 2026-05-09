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


# --- contract tests --------------------------------------------------------
#
# The frontend codegens against /openapi.json, which is only useful when the
# route declares ``response_model=``. These tests pin both the wire shape and
# the schema so any future drift between code and contract fails CI here
# rather than as a silent client warning at integration time.


def test_response_shape_matches_pydantic_model(clean_capabilities, opts_stub):
    """Response bytes must validate against ``PresetApplyResponse``.

    Catches the 'forgot to wrap the dict in the model' regression: route
    returns a bare dict, FastAPI serialises it, but if a future change
    adds a field that the model doesn't declare it'll either be dropped
    silently (model-driven serialisation) or break the client. Validating
    the JSON against the model proves both shapes agree.
    """
    from modules.cplugapi.preset import PresetApplyResponse

    client = _make_client()
    r = client.post(f"{PREFIX}/forge/preset/sketch")
    assert r.status_code == 200
    PresetApplyResponse.model_validate(r.json())


def test_openapi_schema_describes_preset_response(clean_capabilities, opts_stub):
    """``/openapi.json`` must surface ``PresetApplyResponse`` for the route.

    This is the contract the frontend codegens against. If the route
    drops ``response_model=`` (or someone returns ``dict`` again), the
    schema regresses to the bare-route shape and clients lose typed
    access — failing here is the cheap way to catch it.
    """
    client = _make_client()
    schema = client.get("/openapi.json").json()

    op = schema["paths"][f"{PREFIX}/forge/preset/{{name}}"]["post"]
    response_ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert response_ref.endswith("/PresetApplyResponse"), (
        f"expected $ref to PresetApplyResponse, got {response_ref!r}"
    )

    component = schema["components"]["schemas"]["PresetApplyResponse"]
    assert set(component["properties"].keys()) == {"preset", "applied"}
    # ``applied`` is intentionally an open object (heterogeneous values
    # by preset). Asserting ``type: object`` keeps clients honest about
    # not assuming bool / scalar.
    assert component["properties"]["applied"]["type"] == "object"


def test_response_keys_for_sketch_preset(clean_capabilities, opts_stub):
    """The ``applied`` payload for ``sketch`` carries a known set of
    keys. If a future preset change adds or drops one, this test
    surfaces the drift before clients see it."""
    client = _make_client()
    body = client.post(f"{PREFIX}/forge/preset/sketch").json()
    assert set(body["applied"].keys()) == {
        "show_progress_type",
        "show_progress_every_n_steps",
        "token_merging_ratio",
        "token_merging_ratio_hr",
        "cuda_warmup",
    }


def test_response_keys_for_default_preset(clean_capabilities, opts_stub):
    """``default`` preset payload is a strict subset of ``sketch`` —
    no ``cuda_warmup`` (no warmup involved when reverting to RGB
    preview)."""
    client = _make_client()
    body = client.post(f"{PREFIX}/forge/preset/default").json()
    assert set(body["applied"].keys()) == {
        "show_progress_type",
        "show_progress_every_n_steps",
        "token_merging_ratio",
        "token_merging_ratio_hr",
    }
