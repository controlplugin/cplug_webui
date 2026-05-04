"""cplugapi-scoped test fixtures.

The top-level ``tests/conftest.py`` installs lightweight stubs for
``modules.progress`` and ``modules.shared`` so cplugapi imports do not
boot the full WebUI. This file adds **cplugapi-package-scoped** helpers
— specifically, a synthetic-safetensors builder used by the
header_peek + sd_checkpoints + models_disk tests.

The factory signature is treated as frozen — header_peek, models_disk,
sd_checkpoints, and architectures tests all plan against the same
shape, so changes here ripple across the suite.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Optional

import pytest


def _build_safetensors_bytes(
    keys: dict[str, tuple[str, list[int]]],
    metadata: Optional[dict],
) -> bytes:
    """Encode a synthetic safetensors blob: 8-byte header length + JSON.

    ``keys`` maps tensor-name → (dtype-string, shape-as-list). Tensor
    bytes are written as zero-padding sized to ``prod(shape) *
    bytes_per_dtype`` so ``data_offsets`` are coherent. The classifier
    only inspects key names + metadata, but we keep the byte layout
    valid so a real ``safetensors`` library reading the file would
    parse it without error.
    """
    _BYTES_PER_DTYPE = {
        "F16": 2, "BF16": 2, "F32": 4, "F64": 8,
        "F8_E4M3": 1, "F8_E5M2": 1,
        "I8": 1, "U8": 1, "I16": 2, "I32": 4, "I64": 8,
        "BOOL": 1,
    }
    header: dict = {}
    if metadata is not None:
        header["__metadata__"] = metadata
    cursor = 0
    for name, (dtype, shape) in keys.items():
        size = max(1, _BYTES_PER_DTYPE.get(dtype, 1))
        for d in shape:
            size *= max(1, d)
        header[name] = {
            "dtype": dtype,
            "shape": shape,
            "data_offsets": [cursor, cursor + size],
        }
        cursor += size
    header_json = json.dumps(header).encode("utf-8")
    body = b"\x00" * cursor
    return struct.pack("<Q", len(header_json)) + header_json + body


def make_safetensors_file(
    keys: dict[str, tuple[str, list[int]]],
    metadata: Optional[dict],
    tmp_path: Path,
    name: str = "fake.safetensors",
) -> Path:
    """Write a synthetic but format-valid .safetensors file.

    Used by header_peek tests and any test that needs a real on-disk
    file the classifier can read. Returns the file path.

    Note: ``tmp_path`` must be a writable directory (typically pytest's
    ``tmp_path`` fixture). ``name`` defaults to ``fake.safetensors`` —
    pass a different name if the test creates multiple files in one
    directory.
    """
    blob = _build_safetensors_bytes(keys, metadata)
    out = tmp_path / name
    out.write_bytes(blob)
    return out


@pytest.fixture
def safetensors_factory(tmp_path):
    """Return a callable that writes synthetic safetensors files.

    Convenience wrapper so tests can write
    ``f("foo.safetensors", keys={...})`` instead of passing tmp_path.
    """
    def _factory(
        name: str,
        keys: dict[str, tuple[str, list[int]]],
        metadata: Optional[dict] = None,
    ) -> Path:
        return make_safetensors_file(keys, metadata, tmp_path, name=name)
    return _factory


@pytest.fixture
def pickle_factory(tmp_path):
    """Return a callable that writes synthetic pickle-format checkpoints.

    Tests use this to build .ckpt / .pt fixtures that ``pickle_peek``
    can torch.load and classify. ``keys`` is a flat dict of tensor
    name → torch.Tensor. ``wrapper`` decides the wire layout:

    - ``"state_dict"`` (default) — saves ``{"state_dict": keys}``,
      mirroring the A1111-era convention most SD .ckpt files use.
    - ``"model"`` — Lightning-style ``{"model": keys}``.
    - ``"bare"`` — saves ``keys`` directly (clean dump).

    The fixture imports torch lazily so the suite still imports in
    environments where torch is not installed (the offending tests
    will skip via ``pytest.importorskip`` instead of failing at
    collection time).
    """
    import pytest as _pytest

    def _factory(
        name: str,
        keys: dict,
        wrapper: str = "state_dict",
    ) -> Path:
        torch = _pytest.importorskip("torch")
        if wrapper == "state_dict":
            obj = {"state_dict": keys}
        elif wrapper == "model":
            obj = {"model": keys}
        elif wrapper == "bare":
            obj = keys
        else:
            raise ValueError(f"unknown wrapper: {wrapper!r}")
        out = tmp_path / name
        torch.save(obj, out)
        return out
    return _factory
