"""Tests for ``GET /cplugapi/v1/models/sd-checkpoints``."""

from __future__ import annotations

import struct
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


def _stub_checkpoints(infos: list[SimpleNamespace]) -> None:
    stub = types.ModuleType("modules.sd_models")
    stub.checkpoints_list = {info.title: info for info in infos}

    class _Holder:
        def get_sd_model(self):
            class FakeInitialModel:
                pass
            return FakeInitialModel()

    stub.model_data = _Holder()
    sys.modules["modules.sd_models"] = stub


def _info(filename, name=None, title=None, shorthash=None, sha256=None):
    name = name or filename.rsplit("/", 1)[-1]
    return SimpleNamespace(
        filename=filename,
        name=name,
        model_name=name.rsplit(".", 1)[0],
        title=title or f"{name} [{shorthash or '0' * 10}]",
        hash=shorthash[:8] if shorthash else None,
        shorthash=shorthash,
        sha256=sha256,
    )


def test_empty_checkpoints_list(clean_capabilities):
    _stub_checkpoints([])
    models_disk.reset_cache()

    r = _make_client().get(f"{PREFIX}/models/sd-checkpoints")
    assert r.status_code == 200
    assert r.json() == {"checkpoints": [], "available_arches": []}


def test_one_sdxl_checkpoint(safetensors_factory, clean_capabilities):
    p = safetensors_factory(
        "model_a.safetensors",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": ("F16", [4, 4]),
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias": ("F16", [1280]),
        },
        metadata=None,
    )
    _stub_checkpoints([_info(str(p), shorthash="abcd123456", sha256="x" * 64)])
    models_disk.reset_cache()

    r = _make_client().get(f"{PREFIX}/models/sd-checkpoints")
    body = r.json()
    assert r.status_code == 200
    assert len(body["checkpoints"]) == 1
    rec = body["checkpoints"][0]
    assert rec["arch"] == "sdxl"
    assert rec["dtype"] == "F16"
    assert rec["error"] is None
    assert body["available_arches"] == ["sdxl"]


def test_truncated_file_yields_per_file_error(tmp_path, clean_capabilities):
    bad = tmp_path / "broken.safetensors"
    bad.write_bytes(struct.pack("<Q", 4096) + b"{}")  # claims 4096 bytes, has 2

    _stub_checkpoints([_info(str(bad), shorthash="00000000", sha256="0" * 64)])
    models_disk.reset_cache()

    r = _make_client().get(f"{PREFIX}/models/sd-checkpoints")
    body = r.json()
    rec = body["checkpoints"][0]
    assert rec["arch"] == "not_a_checkpoint"
    assert rec["error"]["code"] == "invalid_safetensors"
    assert body["available_arches"] == []


def test_unsupported_extension_gguf(tmp_path, clean_capabilities):
    bad = tmp_path / "weights.gguf"
    bad.write_bytes(b"GGUF-blob")

    _stub_checkpoints([_info(str(bad), shorthash="00000000", sha256="0" * 64)])
    models_disk.reset_cache()

    r = _make_client().get(f"{PREFIX}/models/sd-checkpoints")
    rec = r.json()["checkpoints"][0]
    assert rec["arch"] == "not_a_checkpoint"
    assert rec["error"]["code"] == "unsupported_format"


def test_mixed_batch_per_file_resilience(safetensors_factory, tmp_path, clean_capabilities):
    """One bad file must not 500 the whole listing."""
    sdxl = safetensors_factory(
        "good.safetensors",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": ("F16", [4, 4]),
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias": ("F16", [1280]),
        },
        metadata=None,
    )
    sd15 = safetensors_factory(
        "v1.safetensors",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": ("F16", [4, 4]),
            "model.diffusion_model.output_blocks.11.0.skip_connection.weight": ("F16", [4, 4]),
            "cond_stage_model.transformer.text_model.embeddings.token_embedding.weight": ("F16", [4]),
        },
        metadata=None,
    )
    bad = tmp_path / "broken.safetensors"
    bad.write_bytes(struct.pack("<Q", 99999) + b"x")

    _stub_checkpoints([
        _info(str(sdxl), shorthash="aaaaaaaaaa", sha256="a" * 64),
        _info(str(sd15), shorthash="bbbbbbbbbb", sha256="b" * 64),
        _info(str(bad), shorthash="ccccccccc0", sha256="c" * 64),
    ])
    models_disk.reset_cache()

    r = _make_client().get(f"{PREFIX}/models/sd-checkpoints")
    body = r.json()
    assert r.status_code == 200
    assert len(body["checkpoints"]) == 3
    arches = sorted(rec["arch"] for rec in body["checkpoints"])
    assert arches == ["not_a_checkpoint", "sd15", "sdxl"]
    assert sorted(body["available_arches"]) == ["sd15", "sdxl"]


def test_auth_required_returns_401(clean_capabilities):
    def reject_all():
        raise HTTPException(status_code=401, detail="nope")

    _stub_checkpoints([])
    models_disk.reset_cache()

    client = _make_client(auth_dependency=reject_all)
    assert client.get(f"{PREFIX}/models/sd-checkpoints").status_code == 401
