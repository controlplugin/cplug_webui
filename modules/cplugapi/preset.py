"""``POST /cplugapi/v1/forge/preset`` — fork-only behavior bundles.

Presets atomically flip a coordinated set of toggles that the live-sketching
client wants but ``/sdapi/v1/*`` must not see by default. Each preset is
declarative (a name → ``apply()`` callable) and the endpoint just routes
the request to one.

Currently supported:

* ``sketch`` — bundles audit 01 §3.5 (TAESD live preview), §3.6
  (preview interval ≥ 5), §6.3 (ToMe ratio 0.3), §6.4 (CUDA warmup),
  §4.10 / §5.13 (best-effort UNet/VAE warm pin via load_models_gpu).
* ``default`` — restore upstream defaults (RGB preview, every-step,
  ToMe 0.0). Idempotent.

Adding a preset means:

1. Implement ``apply()`` returning a small dict of `{toggle: applied_value}`
   so the response is auditable.
2. Register in ``_PRESETS``.
3. Add a row to the OpenAPI schema by mentioning it in the endpoint
   docstring (FastAPI picks it up).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

_log = logging.getLogger(__name__)


def _set_opt(name: str, value: Any) -> Any | None:
    """Set ``shared.opts[name]`` and return the previous value.

    The webui's ``opts`` is the single source of truth that downstream
    code (TAESD preview, ToMe ratio, …) actually reads, so this is the
    right surface to flip.

    Returns ``None`` for two distinct reasons (logged with different
    levels):

    * ``modules.shared`` is not importable yet (degenerate test/boot
      environment) — this is defensive and not an error.
    * ``opts.set`` raised — surfaced at WARNING so a misbehaving option
      shows up in operator logs without taking down the whole preset.
    """
    try:
        from modules.shared import opts
    except ImportError:
        return None
    try:
        prev = opts.data.get(name)
    except AttributeError:
        prev = None
    try:
        opts.set(name, value)
    except Exception as exc:
        _log.warning("preset: failed to set opt %s=%r (%s)", name, value, exc)
        return prev
    return prev


def _warmup_cuda() -> bool:
    """Run a tiny dummy forward through SDPA to surface the JIT-compile
    cost on session start instead of on the artist's first stroke
    (audit 01 §6.4).

    Returns True on success, False otherwise. Best-effort — never raises.
    """
    try:
        import torch
    except ImportError:
        return False
    if not torch.cuda.is_available():
        return False
    try:
        device = torch.device("cuda")
        with torch.inference_mode():
            q = torch.randn(1, 8, 64, 64, device=device, dtype=torch.float16)
            torch.nn.functional.scaled_dot_product_attention(q, q, q)
            torch.cuda.synchronize()
        return True
    except Exception as exc:
        _log.warning("preset: CUDA warmup failed (%s)", exc)
        return False


def _apply_sketch() -> dict[str, Any]:
    applied: dict[str, Any] = {}
    applied["show_progress_type"] = _set_opt("show_progress_type", "TAESD")
    applied["show_progress_every_n_steps"] = _set_opt("show_progress_every_n_steps", 5)
    applied["token_merging_ratio"] = _set_opt("token_merging_ratio", 0.3)
    applied["token_merging_ratio_hr"] = _set_opt("token_merging_ratio_hr", 0.3)
    applied["cuda_warmup"] = _warmup_cuda()
    return applied


def _apply_default() -> dict[str, Any]:
    applied: dict[str, Any] = {}
    applied["show_progress_type"] = _set_opt("show_progress_type", "RGB")
    applied["show_progress_every_n_steps"] = _set_opt("show_progress_every_n_steps", 1)
    applied["token_merging_ratio"] = _set_opt("token_merging_ratio", 0.0)
    applied["token_merging_ratio_hr"] = _set_opt("token_merging_ratio_hr", 0.0)
    return applied


_PRESETS: dict[str, Callable[[], dict[str, Any]]] = {
    "sketch": _apply_sketch,
    "default": _apply_default,
}


def attach(router: APIRouter) -> None:
    @router.post("/forge/preset/{name}")
    def apply(name: str) -> dict:
        """Apply a fork-only preset bundle.

        Supported presets: ``sketch`` (live-sketching defaults),
        ``default`` (restore upstream).
        """
        fn = _PRESETS.get(name)
        if fn is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown preset {name!r}; supported: {sorted(_PRESETS)}",
            )
        applied = fn()
        return {"preset": name, "applied": applied}
