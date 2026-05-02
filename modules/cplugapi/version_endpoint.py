"""``GET /cplugapi/v1/version`` — Track 05 §5.3.

Verbose diagnostic dump used by support tickets, telemetry, and the
client's About screen. Distinct from ``/identify`` so the cheap probe
stays cheap.

Cached for 60 s — the underlying values do not change at runtime except
attention-backend / quantization, which are session-scoped.
"""

from __future__ import annotations

import platform
import threading
import time
from typing import Any, Callable

from fastapi import APIRouter

from .__version__ import (
    FORK_BUILD_DATE,
    FORK_COMMIT,
    FORK_NAME,
    FORK_VERSION,
    UPSTREAM_BRANCH,
    UPSTREAM_COMMIT,
    UPSTREAM_NAME,
)

_CACHE_TTL_SECONDS = 60.0


class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._expires_at: float = 0.0

    def get(self, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._payload is None or now >= self._expires_at:
                self._payload = build()
                self._expires_at = now + _CACHE_TTL_SECONDS
            # Return a shallow copy so a top-level mutation by a caller
            # (e.g. extension hook) can't poison subsequent cache hits.
            # Shallow is enough only because no caller mutates the
            # nested ``gpu`` / ``loaded_extensions`` lists today —
            # deepcopy would be wasted work per request.
            return dict(self._payload)

    def invalidate(self) -> None:
        with self._lock:
            self._payload = None
            self._expires_at = 0.0


_cache = _Cache()
reset = _cache.invalidate  # test fixture friendliness


def _safe_module_version(module_name: str) -> str | None:
    try:
        module = __import__(module_name)
        return getattr(module, "__version__", None)
    except (ImportError, AttributeError):
        return None


def _torch_block() -> dict[str, Any]:
    """Best-effort torch / CUDA introspection.

    Built into a local dict that is only merged into the response on
    success — partial-failure (e.g. driver crash mid-build) leaves the
    response with stable ``None`` defaults rather than half-populated
    fields. Phase-3/5/6 fields (attention_backend, active_quantization)
    surface here as ``None`` so the OpenAPI schema is forward-compatible.
    """
    out: dict[str, Any] = {
        "torch_version": None,
        "cuda_version": None,
        "gpu": [],
        "attention_backend": None,
        "active_quantization": None,
    }
    try:
        import torch

        # Commit torch_version immediately so a CUDA-side failure later
        # doesn't suppress the fact we successfully imported torch — the
        # response should reveal as much info as could be collected.
        out["torch_version"] = torch.__version__

        if torch.cuda.is_available():
            out["cuda_version"] = torch.version.cuda
            gpus: list[dict[str, Any]] = []
            for i in range(torch.cuda.device_count()):
                gpus.append(
                    {
                        "name": torch.cuda.get_device_name(i),
                        "vram_total_mb": torch.cuda.get_device_properties(i).total_memory
                        // (1024 * 1024),
                    }
                )
            out["gpu"] = gpus
    except (ImportError, AttributeError, RuntimeError):
        pass
    return out


def _build_payload() -> dict[str, Any]:
    body: dict[str, Any] = {
        "fork": FORK_NAME,
        "fork_version": FORK_VERSION,
        "fork_build_commit": FORK_COMMIT,
        "fork_build_date": FORK_BUILD_DATE,
        "upstream": UPSTREAM_NAME,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_branch": UPSTREAM_BRANCH,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "gradio_version": _safe_module_version("gradio"),
        "fastapi_version": _safe_module_version("fastapi"),
        # Phase-6 forward-compat placeholders. Wired once the relevant
        # tracks land (loaded_extensions: T45 / extensions list scrape).
        "loaded_extensions": [],
    }
    body.update(_torch_block())
    return body


def attach(router: APIRouter) -> None:
    @router.get("/version")
    def version() -> dict:
        return _cache.get(_build_payload)
