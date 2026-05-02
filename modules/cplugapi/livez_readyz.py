"""K8s-style ``/livez`` + ``/readyz`` for ``/cplugapi/v1/*``.

The existing ``/health`` endpoint mixes two concerns: liveness (is the
process responsive?) and readiness (can it actually serve a generation
request?). Operators and orchestrators want them separate:

* ``GET /livez``  — 200 unconditional. Tests the event loop only. No
  model checks, no GPU probes. If this returns the process is alive
  and the FastAPI worker is dispatching.
* ``GET /readyz`` — 200 when the service is ready to accept work:
  torch importable, at least one model checkpoint loaded, no recently
  recorded fatal error. 503 otherwise. Either way the response carries
  a ``checks`` block enumerating which sub-check passed / failed so an
  operator does not have to grep logs.

A module-local last-error registry lets other cplugapi modules surface
fatal conditions to ``readyz`` without coupling through globals — call
:func:`record_last_error` from a failure path and :func:`clear_last_error`
when the condition resolves.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import capabilities

# ---------------------------------------------------------------------------
# Last-error registry
# ---------------------------------------------------------------------------
#
# Holds at most one record (the latest); readiness flips to 503 while a
# record is present. Modules clear the record once the condition is gone.

_lock = threading.Lock()
_last_error: Optional[dict[str, Any]] = None


def record_last_error(kind: str, detail: str) -> None:
    """Record a fatal condition that should fail readiness probes.

    ``kind``   short identifier (e.g. ``"oom"``, ``"checkpoint_load"``).
    ``detail`` human-readable string. Both surface verbatim under
               ``readyz``'s ``checks.last_error``.
    """
    global _last_error
    with _lock:
        _last_error = {
            "kind": kind,
            "detail": detail,
            "recorded_at": time.time(),
        }


def clear_last_error() -> None:
    """Clear the last-error record. No-op if already empty."""
    global _last_error
    with _lock:
        _last_error = None


def get_last_error() -> Optional[dict[str, Any]]:
    """Return a copy of the current last-error record, or ``None``."""
    with _lock:
        return None if _last_error is None else dict(_last_error)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _torch_importable() -> bool:
    try:
        import torch  # noqa: F401

        return True
    except Exception:
        # Catch broadly — a partial torch install can raise OSError on
        # import in some Windows DLL-mismatch scenarios.
        return False


def _model_loaded() -> Optional[bool]:
    """Best-effort model-loaded check.

    Returns ``True`` / ``False`` if we could probe ``modules.shared.opts``,
    or ``None`` when the WebUI hasn't bootstrapped far enough for the
    check to be meaningful (treated as "unknown -> ready" by the caller).
    """
    try:
        from modules import shared
    except Exception:
        return None
    opts = getattr(shared, "opts", None)
    if opts is None:
        return None
    data = getattr(opts, "data", None)
    if data is None:
        return None
    try:
        ckpt = data.get("sd_model_checkpoint")
    except Exception:
        return None
    if ckpt is None:
        return False
    if isinstance(ckpt, str) and not ckpt.strip():
        return False
    return True


def _readyz_payload() -> tuple[int, dict[str, Any]]:
    """Compute readiness. Returns (status_code, body)."""
    torch_ok = _torch_importable()
    model = _model_loaded()
    err = get_last_error()

    checks: dict[str, Any] = {
        "torch_importable": torch_ok,
        # Surface ``None`` as JSON ``null`` — the client can distinguish
        # "I don't know" from "definitely not loaded".
        "model_loaded": model,
        "last_error": err,
    }

    # Treat ``None`` as ready: an environment without ``modules.shared``
    # bootstrapped (early boot, unit tests) is not the readiness probe's
    # job to gate. ``False`` is hard-fail.
    model_ok = model is not False

    ready = torch_ok and model_ok and err is None
    if ready:
        return 200, {"status": "ready", "checks": checks}
    return 503, {"status": "not_ready", "checks": checks}


# ---------------------------------------------------------------------------
# Router wiring
# ---------------------------------------------------------------------------


def attach(router: APIRouter) -> None:
    @router.get("/livez")
    def livez() -> dict:
        # Unconditional. If the event loop is running, this returns.
        return {"status": "live"}

    @router.get("/readyz")
    def readyz() -> JSONResponse:
        status, body = _readyz_payload()
        return JSONResponse(status_code=status, content=body)


def register_capabilities() -> None:
    """Advertise probes. Idempotent."""
    capabilities.register("livez")
    capabilities.register("readyz")
