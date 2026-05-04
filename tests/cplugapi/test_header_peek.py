"""Tests for ``modules.cplugapi.header_peek``."""

from __future__ import annotations

import struct
import sys

import pytest

from modules.cplugapi import header_peek


def test_peek_extracts_keys_metadata_dtypes(safetensors_factory):
    p = safetensors_factory(
        "ok.safetensors",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": ("F16", [4, 4]),
            "first_stage_model.encoder.conv_in.weight": ("F16", [3, 3]),
        },
        metadata={"modelspec.architecture": "stable-diffusion-v1"},
    )
    result = header_peek.peek(p)
    assert "model.diffusion_model.input_blocks.0.0.weight" in result.keys
    assert "first_stage_model.encoder.conv_in.weight" in result.keys
    assert result.metadata == {"modelspec.architecture": "stable-diffusion-v1"}
    assert result.dtypes["model.diffusion_model.input_blocks.0.0.weight"] == "F16"


def test_peek_no_metadata(safetensors_factory):
    p = safetensors_factory(
        "no_md.safetensors",
        keys={"k.weight": ("F32", [1])},
        metadata=None,
    )
    result = header_peek.peek(p)
    assert result.metadata is None


def test_peek_missing_file(tmp_path):
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(tmp_path / "does_not_exist.safetensors")
    assert exc.value.code == "model_not_found"


def test_peek_pickle_extension_gguf(tmp_path):
    p = tmp_path / "weights.gguf"
    p.write_bytes(b"GGUF-binary-blob")
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "pickle_format"


def test_peek_pickle_extension_ckpt(tmp_path):
    p = tmp_path / "legacy.ckpt"
    p.write_bytes(b"PYTORCH-PICKLE")
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "pickle_format"


def test_peek_sharded_manifest_unsupported(tmp_path):
    """Sharded-safetensors manifests are not single-file checkpoints."""
    p = tmp_path / "model.safetensors.index.json"
    p.write_text('{"metadata": {}, "weight_map": {}}', encoding="utf-8")
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "unsupported_format"


def test_peek_too_small_for_header(tmp_path):
    p = tmp_path / "tiny.safetensors"
    p.write_bytes(b"\x00\x01")  # only 2 bytes
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "invalid_safetensors"


def test_peek_zero_length_header(tmp_path):
    p = tmp_path / "zero.safetensors"
    p.write_bytes(struct.pack("<Q", 0) + b"{}")
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "invalid_safetensors"


def test_peek_oversize_header(tmp_path):
    p = tmp_path / "huge.safetensors"
    # Claim header length 200 MiB (above the 100 MiB cap).
    p.write_bytes(struct.pack("<Q", 200 * 1024 * 1024) + b"{}")
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "invalid_safetensors"
    assert "implausible" in exc.value.message


def test_peek_truncated_header(tmp_path):
    """Declared header length exceeds available bytes."""
    p = tmp_path / "truncated.safetensors"
    declared = 4096
    p.write_bytes(struct.pack("<Q", declared) + b"{}")  # only 2 body bytes
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "invalid_safetensors"
    assert "truncated" in exc.value.message


def test_peek_invalid_json(tmp_path):
    p = tmp_path / "bad_json.safetensors"
    body = b"not really json {{["
    p.write_bytes(struct.pack("<Q", len(body)) + body)
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "invalid_safetensors"


def test_peek_json_root_not_object(tmp_path):
    p = tmp_path / "array.safetensors"
    body = b"[1, 2, 3]"
    p.write_bytes(struct.pack("<Q", len(body)) + body)
    with pytest.raises(header_peek.HeaderPeekError) as exc:
        header_peek.peek(p)
    assert exc.value.code == "invalid_safetensors"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics differ on Windows")
def test_peek_permission_denied(safetensors_factory, tmp_path):
    import os
    p = safetensors_factory(
        "locked.safetensors",
        keys={"k.weight": ("F16", [1])},
        metadata=None,
    )
    os.chmod(p, 0)
    try:
        with pytest.raises(header_peek.HeaderPeekError) as exc:
            header_peek.peek(p)
        assert exc.value.code == "permission_denied"
    finally:
        os.chmod(p, 0o644)
