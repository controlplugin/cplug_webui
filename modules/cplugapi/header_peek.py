"""Read the safetensors JSON header without mapping any tensor data.

The safetensors format is documented at
https://huggingface.co/docs/safetensors:

  [0..8)        u64 little-endian = N (length of JSON header in bytes)
  [8..8+N)      UTF-8 JSON: per-tensor metadata + optional __metadata__
  [8+N..EOF)    raw tensor bytes

We need just the JSON portion to enumerate tensor names and dtypes —
that's enough to classify the architecture (see :mod:`arch`). Reading
this way avoids the ``safetensors`` library's mmap, which on Windows
keeps a file handle open past the ``with`` block (preventing the user
from moving / deleting the file until GC kicks in). The local
``modules/sd_models.py::read_metadata_from_safetensors`` uses the same
raw-read pattern; we extend it to return the full key set + dtypes.

Bound: header length capped at 100 MiB (matches ComfyUI). A corrupt or
non-safetensors file reads near-random bytes as the length prefix; the
cap is the only thing standing between us and a 16-EiB ``f.read(n)``.
"""

from __future__ import annotations

import errno
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

# 100 MiB matches comfy/utils.py::safetensors_header. Real headers are
# kilobytes; values larger than this signal a corrupt or wrong-format file.
_MAX_HEADER_BYTES = 100 * 1024 * 1024


class HeaderPeekError(Exception):
    """Raised when a file cannot be peeked.

    ``code`` is one of ``model_not_found``, ``permission_denied``,
    ``invalid_safetensors``, ``unsupported_format`` — chosen so the
    handler can map directly to a JSON error envelope without further
    classification.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HeaderPeek:
    """Result of a successful header read.

    ``keys`` is the set of tensor names (excludes ``__metadata__``).
    ``metadata`` is the ``__metadata__`` dict if present, else ``None``.
    ``dtypes`` is per-tensor dtype string (``F16``, ``BF16``, ``F8_E4M3``,
    etc.) for surfacing FP8/quantized variants in the response payload.
    """

    keys: frozenset[str]
    metadata: Optional[dict]
    dtypes: dict[str, str]


_SAFETENSORS_EXT = ".safetensors"
_UNSUPPORTED_EXTS = (".ckpt", ".pt", ".pth", ".bin", ".gguf")
# HuggingFace sharded-checkpoint manifests use ``<model>.safetensors.index.json``.
# These are JSON, not safetensors — peeking them as if they were would
# parse 8 random JSON bytes as a header length and cap-fail. Reject
# explicitly so the per-file error is informative instead of misleading.
_INDEX_SUFFIX = ".safetensors.index.json"


def _classify_path(p: str) -> Optional[HeaderPeekError]:
    """Pre-flight extension checks. Returns an error to raise, or None
    if the path is plausibly a safetensors file we should attempt to read.
    """
    pl = p.lower()
    if pl.endswith(_INDEX_SUFFIX):
        return HeaderPeekError(
            "unsupported_format",
            "sharded-safetensors manifest (.index.json) is not a single-file checkpoint",
        )
    ext = os.path.splitext(pl)[1]
    if ext in _UNSUPPORTED_EXTS:
        return HeaderPeekError(
            "unsupported_format",
            f"{ext} files are not safetensors; deep parsing intentionally skipped",
        )
    return None


def peek(path: Union[str, Path]) -> HeaderPeek:
    """Read just the JSON header of a safetensors file.

    Raises :class:`HeaderPeekError` on:
      - ``model_not_found`` — file does not exist
      - ``permission_denied`` — open() raised EACCES
      - ``unsupported_format`` — extension is .ckpt / .pt / .gguf or the
        path is a sharded-checkpoint manifest (.safetensors.index.json)
      - ``invalid_safetensors`` — header length implausible, or JSON
        cannot be parsed (truncated / corrupt / wrong format)
    """
    p = os.fspath(path)
    pre = _classify_path(p)
    if pre is not None:
        raise pre

    try:
        with open(p, "rb") as f:
            prefix = f.read(8)
            if len(prefix) < 8:
                raise HeaderPeekError(
                    "invalid_safetensors",
                    f"file too small to contain a safetensors header (got {len(prefix)} bytes)",
                )
            n = struct.unpack("<Q", prefix)[0]
            if n == 0 or n > _MAX_HEADER_BYTES:
                raise HeaderPeekError(
                    "invalid_safetensors",
                    f"implausible header length {n} (cap {_MAX_HEADER_BYTES})",
                )
            body = f.read(n)
            if len(body) < n:
                # Truncated file (download in progress, partial copy, ...).
                raise HeaderPeekError(
                    "invalid_safetensors",
                    f"truncated header: declared {n} bytes, got {len(body)}",
                )
    except FileNotFoundError as exc:
        raise HeaderPeekError("model_not_found", str(exc)) from exc
    except PermissionError as exc:
        raise HeaderPeekError("permission_denied", str(exc)) from exc
    except OSError as exc:
        # EACCES on some platforms surfaces as OSError rather than
        # PermissionError; EISDIR if the path is a directory; etc.
        if exc.errno == errno.EACCES:
            raise HeaderPeekError("permission_denied", str(exc)) from exc
        raise HeaderPeekError("invalid_safetensors", str(exc)) from exc

    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HeaderPeekError(
            "invalid_safetensors", f"header is not valid JSON: {exc}"
        ) from exc

    if not isinstance(obj, dict):
        raise HeaderPeekError(
            "invalid_safetensors", "header JSON root is not an object"
        )

    raw_md = obj.get("__metadata__")
    metadata = raw_md if isinstance(raw_md, dict) else None
    keys = frozenset(k for k in obj if k != "__metadata__")
    dtypes = {
        k: v.get("dtype", "")
        for k, v in obj.items()
        if k != "__metadata__" and isinstance(v, dict)
    }
    return HeaderPeek(keys=keys, metadata=metadata, dtypes=dtypes)
