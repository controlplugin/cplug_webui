"""``GET /cplugapi/v1/version`` — Track 05 §5.3.

Verbose diagnostic dump used by support tickets, telemetry, and the
client's About screen. Distinct from ``/identify`` so the cheap probe
stays cheap.

Cached for 60 s — the underlying values do not change at runtime except
attention-backend / quantization, which are session-scoped.
"""

from __future__ import annotations

import datetime
import platform
import threading
import time
from typing import Any

from fastapi import APIRouter

from .__version__ import (
    FORK_COMMIT,
    FORK_NAME,
    FORK_VERSION,
    UPSTREAM_COMMIT,
    UPSTREAM_NAME,
)

_CACHE_TTL_SECONDS = 60.0


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._expires_at: float = 0.0

    def get(self, build: "_Builder") -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._payload is None or now >= self._expires_at:
                self._payload = build()
                self._expires_at = now + _CACHE_TTL_SECONDS
            return self._payload

    def invalidate(self) -> None:
        with self._lock:
            self._payload = None
            self._expires_at = 0.0


_Builder = Any  # callable returning dict; avoid TYPE_CHECKING import dance
_cache = _Cache()


def _safe_module_version(module_name: str) -> str | None:
    try:
        module = __import__(module_name)
        return getattr(module, "__version__", None)
    except Exception:
        return None


def _torch_block() -> dict[str, Any]:
    out: dict[str, Any] = {
        "torch_version": None,
        "cuda_version": None,
        "gpu": [],
    }
    try:
        import torch

        out["torch_version"] = torch.__version__
        if torch.cuda.is_available():
            out["cuda_version"] = torch.version.cuda
            out["gpu"] = [
                {
                    "name": torch.cuda.get_device_name(i),
                    "vram_total_mb": torch.cuda.get_device_properties(i).total_memory
                    // (1024 * 1024),
                }
                for i in range(torch.cuda.device_count())
            ]
    except Exception:
        pass
    return out


def _build_payload() -> dict[str, Any]:
    body: dict[str, Any] = {
        "fork": FORK_NAME,
        "fork_version": FORK_VERSION,
        "fork_build_commit": FORK_COMMIT,
        "fork_build_date": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "upstream": UPSTREAM_NAME,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_branch": "neo",
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "gradio_version": _safe_module_version("gradio"),
        "fastapi_version": _safe_module_version("fastapi"),
    }
    body.update(_torch_block())
    return body


def attach(router: APIRouter) -> None:
    @router.get("/version")
    def version() -> dict:
        return _cache.get(_build_payload)
