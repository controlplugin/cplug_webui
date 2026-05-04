"""Tests for ``GET /cplugapi/v1/models/architectures`` (lightweight summary)."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, setup_cplugapi
from modules.cplugapi import models_disk


def _make_client(auth_dependency=None):
    app = FastAPI()
    setup_cplugapi(app, auth_dependency=auth_dependency)
    return TestClient(app)


def _stub_checkpoints(infos):
    stub = types.ModuleType("modules.sd_models")
    stub.checkpoints_list = {info.title: info for info in infos}

    class _Holder:
        def get_sd_model(self):
            class FakeInitialModel:
                pass
            return FakeInitialModel()

    stub.model_data = _Holder()
    sys.modules["modules.sd_models"] = stub


def _info(filename, shorthash):
    name = filename.rsplit("/", 1)[-1]
    return SimpleNamespace(
        filename=filename, name=name,
        model_name=name.rsplit(".", 1)[0],
        title=f"{name} [{shorthash}]",
        hash=shorthash[:8], shorthash=shorthash, sha256="x" * 64,
    )


def test_empty(clean_capabilities):
    _stub_checkpoints([])
    models_disk.reset_cache()
    r = _make_client().get(f"{PREFIX}/models/architectures")
    assert r.status_code == 200
    assert r.json() == {"available_arches": []}


def test_mixed_dir_returns_unique_sorted(safetensors_factory, clean_capabilities):
    sdxl_a = safetensors_factory(
        "a.safetensors",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": ("F16", [4, 4]),
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias": ("F16", [1280]),
        },
        metadata=None,
    )
    sdxl_b = safetensors_factory(
        "b.safetensors",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": ("F16", [4, 4]),
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias": ("F16", [1280]),
        },
        metadata=None,
    )
    flux = safetensors_factory(
        "flux.safetensors",
        keys={
            "double_blocks.0.img_attn.qkv.weight": ("F16", [4, 4]),
            "img_in.weight": ("F16", [4, 4]),
            "guidance_in.in_layer.weight": ("F16", [4, 4]),
        },
        metadata=None,
    )
    _stub_checkpoints([
        _info(str(sdxl_a), "aaaaaaaaaa"),
        _info(str(sdxl_b), "bbbbbbbbbb"),
        _info(str(flux), "cccccccccc"),
    ])
    models_disk.reset_cache()

    r = _make_client().get(f"{PREFIX}/models/architectures")
    assert r.status_code == 200
    assert r.json() == {"available_arches": ["flux", "sdxl"]}


def test_excludes_unknown_and_not_a_checkpoint(safetensors_factory, tmp_path, clean_capabilities):
    """Mode picker should ignore unrecognized files."""
    import struct
    sdxl = safetensors_factory(
        "ok.safetensors",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": ("F16", [4, 4]),
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias": ("F16", [1280]),
        },
        metadata=None,
    )
    bad = tmp_path / "bad.safetensors"
    bad.write_bytes(struct.pack("<Q", 99999) + b"x")

    _stub_checkpoints([
        _info(str(sdxl), "aaaaaaaaaa"),
        _info(str(bad), "bbbbbbbbbb"),
    ])
    models_disk.reset_cache()

    r = _make_client().get(f"{PREFIX}/models/architectures")
    assert r.json() == {"available_arches": ["sdxl"]}


def test_auth_required_returns_401(clean_capabilities):
    def reject_all():
        raise HTTPException(status_code=401, detail="nope")
    _stub_checkpoints([])
    models_disk.reset_cache()
    client = _make_client(auth_dependency=reject_all)
    assert client.get(f"{PREFIX}/models/architectures").status_code == 401
