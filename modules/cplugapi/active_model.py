"""``GET /cplugapi/v1/models/active`` — arch of the currently-loaded model.

Reads what Forge's loader has already decided. The diffusion engine
classes in ``backend/diffusion_engine/*.py`` are a 1:1 mapping to arch
labels (with one exception: ``Flux`` covers both flux-dev and
flux-schnell — endpoint (1) returns the coarse ``"flux"`` and clients
needing the variant cross-ref via ``/cplugapi/v1/models/sd-checkpoints``).

Always returns 200. ``loaded=false`` is a state, not an error — the
WebUI may legitimately boot without a model selected.

``engine_class`` is **diagnostic-only**. Clients MUST gate on ``arch``;
a future Forge rename of an engine class is allowed to flip
``engine_class`` without breaking the contract.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter

from . import arch as _arch
from . import capabilities

_log = logging.getLogger(__name__)

_UNLOADED_PAYLOAD = {
    "loaded": False,
    "arch": None,
    "engine_class": None,
    "checkpoint": None,
    "checkpoint_hash": None,
    "checkpoint_sha256": None,
}


def _basename(name: str) -> Optional[str]:
    """Return the filename component, normalising Windows separators.
    Returns ``None`` if the input is empty after splitting (defensive —
    upstream sometimes hands us paths ending in a separator)."""
    if not name:
        return None
    tail = name.replace("\\", "/").rsplit("/", 1)[-1]
    return tail or None


def _build_payload() -> dict:
    """Produce the response dict. Lazy-imports sd_models so the module
    is importable in tests that stub it via sys.modules.
    """
    from modules import sd_models

    try:
        engine = sd_models.model_data.get_sd_model()
    except Exception:
        # Loader in mid-failure (CUDA OOM, partial state). Surface as
        # "nothing loaded" rather than 500ing — clients can poll for
        # readiness and the user can re-pick a model.
        _log.warning("active_model: get_sd_model raised", exc_info=True)
        return dict(_UNLOADED_PAYLOAD)

    cls_name = type(engine).__name__
    if cls_name == "FakeInitialModel":
        return dict(_UNLOADED_PAYLOAD)

    info = getattr(engine, "sd_checkpoint_info", None)
    return {
        "loaded": True,
        "arch": _arch.arch_for_engine(engine),
        "engine_class": cls_name,
        "checkpoint": _basename(getattr(info, "name", None) or ""),
        "checkpoint_hash": getattr(info, "shorthash", None),
        "checkpoint_sha256": getattr(info, "sha256", None),
    }


def attach(router: APIRouter) -> None:
    @router.get("/models/active")
    def active_model() -> dict:
        return _build_payload()


def register_capabilities() -> None:
    capabilities.register("models/architecture")
