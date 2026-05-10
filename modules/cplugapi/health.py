"""``GET /cplugapi/v1/health`` — Track 05 §5.2.

Lock-free liveness + capability advertisement. ``?detailed=true`` adds a
best-effort diagnostic block (VRAM + Phase-3/5/6 forward-compat
placeholders) that pulls from existing Forge Neo internals.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from . import capabilities

# When ``len(pending_tasks) >= _BUSY_QUEUE_DEPTH`` the status flips from
# "ok" to "busy" so clients can throttle. Single-user desktop deployments
# rarely cross 1; threshold is conservative. ``"degraded"`` is reserved
# for Phase 5/6 (T33 warm-pool eviction failures, T42 #936 detection,
# SageAttention fallback) — wire here when those land.
_BUSY_QUEUE_DEPTH = 3


def _basic_payload() -> dict[str, Any]:
    # Local imports keep the module importable in environments where the
    # full webui hasn't bootstrapped yet (e.g. unit tests).
    from modules import progress

    queue_depth = len(progress.pending_tasks)
    status = "busy" if queue_depth >= _BUSY_QUEUE_DEPTH else "ok"

    return {
        "status": status,
        "capabilities": capabilities.enabled_capabilities(),
        "deprecated_capabilities": capabilities.deprecated_capabilities(),
        "active_task_id": progress.current_task,
        "queue_depth": queue_depth,
    }


def _detailed_block() -> dict[str, Any]:
    """Best-effort diagnostic block with forward-compat placeholders.

    Phase-3/5/6 fields surface as ``null`` / ``[]`` / ``False`` so the
    OpenAPI schema stays stable across phase boundaries — clients can
    rely on the keys being present.
    """
    out: dict[str, Any] = {
        "vram_used_mb": None,
        "vram_total_mb": None,
        "warm_pool_slots": [],  # Phase 3 (T33-T38)
        "active_attention_backend": None,  # Phase 6 polish
        "comfy_finalization_tax_active": False,  # Phase 5 (T42 detection)
    }
    try:
        import torch

        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            out["vram_used_mb"] = (total - free) // (1024 * 1024)
            out["vram_total_mb"] = total // (1024 * 1024)
    except (ImportError, AttributeError, RuntimeError):
        # Detailed mode is best-effort; never let it 500 a liveness probe.
        pass
    return out


def attach(router: APIRouter) -> None:
    @router.get("/health")
    def health(detailed: bool = False) -> dict:
        body = _basic_payload()
        if detailed:
            body.update(_detailed_block())
        return body
