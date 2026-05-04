"""Tests for ``modules.cplugapi.pickle_peek``.

Pickle peek extracts state-dict shape from .ckpt/.pt/.pth/.bin via
``torch.load(weights_only=True, map_location="meta")``. Fixtures use
real torch.save output rather than hand-crafted bytes — the format
isn't worth re-implementing in test code, and round-tripping through
torch ensures we test the same bytes a real Forge model would produce.
"""

from __future__ import annotations

import pytest

from modules.cplugapi import pickle_peek
from modules.cplugapi.header_peek import HeaderPeekError


def _t(dtype: str = "float16"):
    """Tiny meta-tensor for state-dict construction. Shape doesn't
    matter — classifier only inspects keys."""
    torch = pytest.importorskip("torch")
    return torch.zeros(2, 2, dtype=getattr(torch, dtype))


def test_peek_classifies_sdxl_via_ckpt(pickle_factory):
    """Round-trip an SDXL state-dict through .ckpt — keys must reach
    the classifier intact so the same arch label drops out."""
    p = pickle_factory(
        "sdxl_legacy.ckpt",
        keys={
            "model.diffusion_model.input_blocks.0.0.weight": _t(),
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias": _t(),
        },
    )
    peeked = pickle_peek.peek(p)
    assert "model.diffusion_model.input_blocks.0.0.weight" in peeked.keys
    # Dtype mapping: torch.float16 -> safetensors-style "F16".
    sample_dtype = peeked.dtypes["model.diffusion_model.input_blocks.0.0.weight"]
    assert sample_dtype == "F16"


def test_peek_unwraps_model_key(pickle_factory):
    """Lightning-style {"model": ...} wrapper must unwrap."""
    p = pickle_factory(
        "lightning.ckpt",
        keys={"input_blocks.0.0.weight": _t()},
        wrapper="model",
    )
    peeked = pickle_peek.peek(p)
    assert "input_blocks.0.0.weight" in peeked.keys


def test_peek_handles_bare_state_dict(pickle_factory):
    """Clean dumps without a wrapper key must still classify."""
    p = pickle_factory(
        "bare.pt",
        keys={"input_blocks.0.0.weight": _t()},
        wrapper="bare",
    )
    peeked = pickle_peek.peek(p)
    assert "input_blocks.0.0.weight" in peeked.keys


def test_peek_pt_extension(pickle_factory):
    p = pickle_factory("dump.pt", keys={"input_blocks.0.0.weight": _t()})
    assert "input_blocks.0.0.weight" in pickle_peek.peek(p).keys


def test_peek_pth_extension(pickle_factory):
    p = pickle_factory("dump.pth", keys={"input_blocks.0.0.weight": _t()})
    assert "input_blocks.0.0.weight" in pickle_peek.peek(p).keys


def test_peek_bin_extension(pickle_factory):
    p = pickle_factory("dump.bin", keys={"input_blocks.0.0.weight": _t()})
    assert "input_blocks.0.0.weight" in pickle_peek.peek(p).keys


def test_peek_rejects_gguf_with_specific_code(tmp_path):
    """``.gguf`` is a different format; we never attempt torch.load.
    The error code must distinguish "we tried and failed" from
    "we don't handle this format" — the desktop client renders these
    differently."""
    p = tmp_path / "weights.gguf"
    p.write_bytes(b"GGUF\x00binary-blob")
    with pytest.raises(HeaderPeekError) as exc:
        pickle_peek.peek(p)
    assert exc.value.code == "gguf_unsupported"


def test_peek_rejects_unrecognised_extension(tmp_path):
    p = tmp_path / "weights.exe"
    p.write_bytes(b"\x00\x00")
    with pytest.raises(HeaderPeekError) as exc:
        pickle_peek.peek(p)
    assert exc.value.code == "unsupported_format"


def test_peek_corrupt_pickle(tmp_path):
    """Random bytes with .ckpt extension must fail cleanly with
    pickle_parse_failed, not crash."""
    p = tmp_path / "garbage.ckpt"
    p.write_bytes(b"this is not a pickle stream")
    with pytest.raises(HeaderPeekError) as exc:
        pickle_peek.peek(p)
    assert exc.value.code == "pickle_parse_failed"


def test_peek_empty_state_dict(pickle_factory):
    """A non-error pickle that resolves to an empty state-dict is
    treated as a parse failure — there's nothing to classify."""
    p = pickle_factory("empty.ckpt", keys={}, wrapper="bare")
    with pytest.raises(HeaderPeekError) as exc:
        pickle_peek.peek(p)
    assert exc.value.code == "pickle_parse_failed"


def test_peek_dtype_normalization(pickle_factory):
    """Mixed dtypes round-trip through the safetensors-style label map."""
    p = pickle_factory(
        "mixed.ckpt",
        keys={
            "a": _t("float16"),
            "b": _t("bfloat16"),
            "c": _t("float32"),
        },
    )
    peeked = pickle_peek.peek(p)
    assert peeked.dtypes["a"] == "F16"
    assert peeked.dtypes["b"] == "BF16"
    assert peeked.dtypes["c"] == "F32"
