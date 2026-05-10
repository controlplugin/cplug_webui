"""K8s-style ``/livez`` + ``/readyz`` for ``/cplugapi/v1/*``.

Both probes are mounted on the **public** router so cloud orchestrators
(k8s liveness/readiness, AWS ALB, GCP load balancer) can poll them
without injecting Basic-auth headers — the fork's posture before W1
auth-gated them, defeating the entire point of standardised probes.

Two probes, two concerns:

* ``GET /livez``  — 200 unconditional. Tests the event loop only. No
  model checks, no GPU probes. Returns ``{"status": "live"}`` — zero
  internal-state leak.
* ``GET /readyz`` — 200 when the service is ready to accept work:
  torch importable, at least one model checkpoint loaded, no recently
  recorded fatal error, not currently draining (W12). 503 otherwise.

The default ``/readyz`` body is **sanitised for unauthenticated
probes** — booleans only, no error detail, no checkpoint paths, no
GPU/VRAM numbers. ``?verbose=1`` lifts the sanitisation but requires
auth (when ``--api-auth`` is configured); operators get the full
diagnostic block, k8s probes get the booleans they need.

A module-local last-error registry lets other cplugapi modules surface
fatal conditions to ``readyz`` without coupling through globals — call
:func:`record_last_error` from a failure path and :func:`clear_last_error`
when the condition resolves.

A separate ``set_draining`` / ``clear_draining`` pair lets W12's
graceful-shutdown handler flip the readiness probe to 503 with
``checks.draining=true`` so orchestrators pull the pod from rotation
during a rolling restart. The ``draining`` flag is part of the
sanitised public body — operational state, not a leak vector.
"""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

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
               ``readyz``'s ``checks.last_error`` *only* on the verbose
               (auth-gated) path. The unauth body reports
               ``has_error: true`` instead.
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
# Draining flag (W12 hook)
# ---------------------------------------------------------------------------
#
# W12's graceful-shutdown handler will flip this on receipt of SIGTERM.
# Until W12 lands, the flag stays False and the public probe always
# reports ``draining: false``. Centralising the flag here means W12
# doesn't have to thread state through readiness logic — it just calls
# ``set_draining(True)``.

_draining_lock = threading.Lock()
_draining = False


def set_draining(value: bool) -> None:
    """Flip the drain flag. Visible immediately to ``/readyz``.

    Called by W12's shutdown handler. Idempotent."""
    global _draining
    with _draining_lock:
        _draining = bool(value)


def clear_draining() -> None:
    """Equivalent to ``set_draining(False)``. Provided for symmetry with
    :func:`clear_last_error`."""
    set_draining(False)


def is_draining() -> bool:
    """Return current drain state."""
    with _draining_lock:
        return _draining


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


def _readyz_payload(verbose: bool) -> tuple[int, dict[str, Any]]:
    """Compute readiness. Returns (status_code, body).

    ``verbose=False`` produces the sanitised public body — booleans
    only. ``verbose=True`` produces the diagnostic body — full
    ``last_error`` record. The caller is responsible for gating
    verbose access on auth.
    """
    torch_ok = _torch_importable()
    model = _model_loaded()
    err = get_last_error()
    draining = is_draining()

    has_error = err is not None
    # Treat ``None`` (model state unknown) as ready: an environment
    # without ``modules.shared`` bootstrapped (early boot, unit tests)
    # is not the readiness probe's job to gate. ``False`` is hard-fail.
    model_ok = model is not False
    ready = torch_ok and model_ok and not has_error and not draining

    if verbose:
        checks: dict[str, Any] = {
            "torch_importable": torch_ok,
            "model_loaded": model,
            "last_error": err,
            "draining": draining,
        }
    else:
        checks = {
            "torch_importable": torch_ok,
            "model_loaded": model,
            "has_error": has_error,
            "draining": draining,
        }

    if ready:
        return 200, {"status": "ready", "checks": checks}
    return 503, {"status": "not_ready", "checks": checks}


# ---------------------------------------------------------------------------
# Manual auth re-entry for ?verbose=1
# ---------------------------------------------------------------------------
#
# ``/readyz`` is mounted on the public router so unauth probes work.
# When ``?verbose=1`` is passed AND ``--api-auth`` is configured, we
# manually invoke the same auth dependency that gates ``/sdapi/v1/*``
# and the cplugapi private router. The dependency expects an
# ``HTTPBasicCredentials`` argument (FastAPI's ``Depends(HTTPBasic())``
# normally provides this), so we parse the header ourselves.


def _parse_basic_credentials(request: Request) -> HTTPBasicCredentials:
    """Decode the ``Authorization: Basic ...`` header. Raises 401 on
    absence or malformed input — same contract as
    ``HTTPBasic(auto_error=True)``."""
    raw = request.headers.get("Authorization", "")
    if not raw or not raw.lower().startswith("basic "):
        raise HTTPException(
            status_code=401,
            detail="authentication required for verbose readyz",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        decoded = base64.b64decode(raw[6:].strip()).decode("utf-8")
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="invalid Basic credentials encoding",
            headers={"WWW-Authenticate": "Basic"},
        )
    if ":" not in decoded:
        raise HTTPException(
            status_code=401,
            detail="invalid Basic credentials format",
            headers={"WWW-Authenticate": "Basic"},
        )
    user, _, password = decoded.partition(":")
    return HTTPBasicCredentials(username=user, password=password)


# ---------------------------------------------------------------------------
# Router wiring
# ---------------------------------------------------------------------------


def attach(
    router: APIRouter,
    auth_dependency: Optional[Callable] = None,
) -> None:
    """Attach ``/livez`` and ``/readyz`` to ``router`` (typically the
    public sub-router so probes reach orchestrators without auth).

    ``auth_dependency`` mirrors :func:`router.setup_cplugapi`'s argument
    — it gates the optional ``?verbose=1`` detail on the same Basic-auth
    posture as the rest of the surface. Pass ``None`` (default) when no
    auth is configured; verbose detail is then unrestricted.
    """

    @router.get("/livez")
    def livez() -> dict:
        # Unconditional. If the event loop is running, this returns.
        return {"status": "live"}

    @router.get("/readyz")
    def readyz(
        request: Request,
        verbose: bool = Query(
            False,
            description=(
                "Include diagnostic detail (last_error record). "
                "Requires Basic auth when --api-auth is set."
            ),
        ),
    ) -> JSONResponse:
        if verbose and auth_dependency is not None:
            # Re-invoke the same auth dep that gates /health, /version,
            # etc. — manually, because FastAPI Depends() can't be
            # conditional on a query param at route-declaration time.
            credentials = _parse_basic_credentials(request)
            auth_dependency(credentials)
        status, body = _readyz_payload(verbose=verbose)
        return JSONResponse(status_code=status, content=body)


def register_capabilities() -> None:
    """Advertise probes. Idempotent.

    W15 — dual-emits ``health/livez`` + ``livez`` (legacy) and
    ``health/readyz`` + ``readyz`` (legacy). Both probe names survive
    one minor release of dual emission; the legacy flat strings are
    dropped after the Rust client confirms migration."""
    capabilities.register_with_legacy(
        new_name="health/livez", legacy_name="livez",
    )
    capabilities.register_with_legacy(
        new_name="health/readyz", legacy_name="readyz",
    )
