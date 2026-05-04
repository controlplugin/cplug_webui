"""State-dict key enumeration for PyTorch pickle checkpoints.

The safetensors header peek (:mod:`header_peek`) is fast and stdlib-only
but applies only to ``.safetensors``. The legacy A1111-era SD ecosystem
shipped models in PyTorch's pickle format (``.ckpt``, ``.pt``, ``.pth``,
sometimes ``.bin``); plenty of them are still in active circulation, so
classifying them is not optional.

Approach: ``torch.load`` with the modern safe options:

- ``weights_only=True`` (PyTorch 2.0+) — restricted unpickler that
  refuses arbitrary callables. Safe to point at untrusted files; refuses
  to call ``__reduce__`` against anything outside a small allowlist.
- ``map_location="meta"`` — tensors are materialized on the ``meta``
  device, which records shape/dtype but allocates no memory. Avoids
  pulling the (multi-GB) tensor data into RAM.
- ``mmap=True`` (PyTorch 2.1+) — when supported, reads pickle index by
  mmap rather than full read. Best-effort; falls back transparently.

Cost: hundreds of ms per cold scan, vs ~ms for safetensors. The LRU
cache (:mod:`models_disk`) amortizes this — only first scan pays.

Out of scope: ``.gguf`` (binary tensor format used by ComfyUI's GGUF
node, mostly Flux quantized variants). Different file layout, different
parser. Surfaced as ``gguf_unsupported`` so the desktop client knows
the error category without inferring from the filename.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Union

from .header_peek import HeaderPeek, HeaderPeekError

_log = logging.getLogger(__name__)

# Extensions this module accepts. Anything else (including ``.gguf``)
# is rejected pre-flight with a specific error code so the caller knows
# why peek didn't even attempt the load.
_PICKLE_EXTS = (".ckpt", ".pt", ".pth", ".bin")
_GGUF_EXT = ".gguf"

# Common state-dict wrapper keys. Order matters: SD checkpoints from
# the A1111 era almost universally use ``state_dict``; lightning-trained
# variants sometimes use ``model``. If the loaded object IS the
# state-dict (clean dump from a custom export), the loop falls through
# and we use the top-level mapping.
_WRAPPER_KEYS = ("state_dict", "model")

# Map torch dtype reprs to safetensors-style names so the response
# field is consistent across formats. ``str(torch.float16)`` returns
# ``"torch.float16"`` — we strip the prefix and map.
_DTYPE_MAP = {
    "torch.float16": "F16",
    "torch.bfloat16": "BF16",
    "torch.float32": "F32",
    "torch.float64": "F64",
    "torch.float8_e4m3fn": "F8_E4M3",
    "torch.float8_e5m2": "F8_E5M2",
    "torch.int8": "I8",
    "torch.int16": "I16",
    "torch.int32": "I32",
    "torch.int64": "I64",
    "torch.uint8": "U8",
    "torch.bool": "BOOL",
}


def _normalize_dtype(torch_dtype: Any) -> str:
    """Convert a torch.dtype to a safetensors-style label, or empty."""
    return _DTYPE_MAP.get(str(torch_dtype), "")


def _unwrap_state_dict(obj: Any) -> Optional[dict]:
    """Walk common wrapper layouts to find the actual state-dict.

    Returns the unwrapped mapping or ``None`` if the file's top-level
    object isn't a dict-like structure we recognise. We deliberately
    DO NOT recurse arbitrarily deep — a checkpoint that nests state
    dicts more than one level is non-standard enough that "unknown"
    is more honest than guessing.
    """
    if not isinstance(obj, dict):
        return None
    for wrapper in _WRAPPER_KEYS:
        inner = obj.get(wrapper)
        if isinstance(inner, dict) and inner:
            return inner
    return obj


def peek(path: Union[str, Path]) -> HeaderPeek:
    """Read a pickle-format checkpoint's state-dict shape only.

    Returns a :class:`HeaderPeek` with the tensor names and dtypes
    populated; ``metadata`` is always ``None`` (pickle has no native
    metadata block). Raises :class:`HeaderPeekError` with one of:

    - ``model_not_found`` / ``permission_denied`` — pre-stat already
      handles these in :mod:`models_disk`, but kept defensive here so
      direct callers get the same contract.
    - ``gguf_unsupported`` — path is ``.gguf``; this module does not
      parse GGUF and never will (different format, separate concern).
    - ``unsupported_format`` — extension is not pickle and not GGUF.
    - ``pickle_parse_failed`` — torch.load raised, or the loaded object
      didn't look like a state-dict. Exception detail goes into
      ``message`` so support tickets have something to grep.
    """
    p = os.fspath(path)
    pl = p.lower()
    if pl.endswith(_GGUF_EXT):
        raise HeaderPeekError(
            "gguf_unsupported",
            ".gguf classification is not implemented; arch detection from "
            "GGUF requires a separate binary-header reader",
        )
    ext = os.path.splitext(pl)[1]
    if ext not in _PICKLE_EXTS:
        raise HeaderPeekError(
            "unsupported_format",
            f"{ext} is not a recognised pickle-format extension",
        )

    try:
        import torch
    except ImportError as exc:
        # Defensive — Forge always has torch, but the cplugapi modules
        # are also imported by the OpenAPI export script (which stubs
        # out heavy deps). Surface a clean error rather than crashing.
        raise HeaderPeekError(
            "pickle_parse_failed",
            f"torch unavailable in this environment: {exc}",
        ) from exc

    try:
        # mmap was added in 2.1. Older torch silently rejects the kwarg
        # in some patch versions; try-with then retry-without keeps the
        # cplugapi module compatible across the Forge-supported range.
        try:
            obj = torch.load(p, map_location="meta", weights_only=True, mmap=True)
        except TypeError:
            obj = torch.load(p, map_location="meta", weights_only=True)
    except FileNotFoundError as exc:
        raise HeaderPeekError("model_not_found", str(exc)) from exc
    except PermissionError as exc:
        raise HeaderPeekError("permission_denied", str(exc)) from exc
    except Exception as exc:
        # torch.load can raise UnpicklingError, RuntimeError,
        # _pickle.UnpicklingError, OSError, RuntimeError("PytorchStreamReader
        # failed reading zip archive"), and weights_only=True specifically
        # raises a long-form error naming the rejected global. All of
        # these mean "we couldn't classify"; client behaviour is the same.
        raise HeaderPeekError(
            "pickle_parse_failed",
            f"torch.load failed: {type(exc).__name__}: {exc}",
        ) from exc

    sd = _unwrap_state_dict(obj)
    if sd is None or not sd:
        raise HeaderPeekError(
            "pickle_parse_failed",
            "loaded object is not a non-empty state-dict",
        )

    keys: list[str] = []
    dtypes: dict[str, str] = {}
    for k, v in sd.items():
        if not isinstance(k, str):
            # Non-string keys exist in the wild (training-state stashes
            # ints / tuples). Skip silently — they can't be classifier
            # sentinels anyway.
            continue
        keys.append(k)
        dt = getattr(v, "dtype", None)
        if dt is not None:
            dtypes[k] = _normalize_dtype(dt)

    if not keys:
        raise HeaderPeekError(
            "pickle_parse_failed",
            "state-dict has no string-keyed tensor entries",
        )

    return HeaderPeek(keys=frozenset(keys), metadata=None, dtypes=dtypes)
