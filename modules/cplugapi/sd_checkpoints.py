"""``GET /cplugapi/v1/models/sd-checkpoints`` — disk-scan with arch tag.

Strict superset of ``/sdapi/v1/sd-models``: same fields plus ``arch``,
``dtype``, ``error``, and a top-level ``available_arches`` summary used
by the ControlPlugin mode picker to grey out arches with zero models.

Per-file resilience: a single corrupt safetensors never 500s the whole
listing. Each file's record carries its own ``error`` (or ``null`` on
success) so the client sees a complete enumeration with per-file
status.

Cache: this slice ships uncached; per-file peek runs every call. Slice
5 (``models_disk.py``) layers an LRU cache without changing the contract.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request

from . import arch as _arch
from . import capabilities
from . import models_disk

_log = logging.getLogger(__name__)

# Arch labels excluded from ``available_arches`` summary (they don't
# correspond to selectable modes in the client).
_NON_MODE_ARCHES = frozenset({_arch.ARCH_UNKNOWN, _arch.ARCH_NOT_A_CHECKPOINT})


def _basename(name: Optional[str]) -> str:
    """Filename component, normalising Windows separators. Empty in,
    empty out — never raises so a corrupt CheckpointInfo doesn't 500
    the whole listing."""
    if not name:
        return ""
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def enumerate_checkpoints(request: Optional[Request]) -> list[dict]:
    """Iterate ``checkpoints_list`` safely and decorate each entry.

    Snapshot via ``list(...)`` — CPython raises ``RuntimeError:
    dictionary changed size during iteration`` if ``list_models()`` runs
    concurrently (e.g., user triggers a model rescan from the Gradio UI
    mid-request). The GIL protects single bytecode ops, NOT multi-step
    iterators.

    Public to the package (no leading underscore) because
    ``architectures.py`` shares this enumeration to derive its summary
    response — both endpoints are siblings of one underlying scan.
    """
    from modules import sd_models

    snapshot = list(sd_models.checkpoints_list.values())
    request_id = (
        getattr(request.state, "request_id", None) if request is not None else None
    )

    out: list[dict] = []
    for info in snapshot:
        arch_info = models_disk.get_arch_info(info.filename)
        if arch_info.error is not None:
            _log.warning(
                "arch_detection_failed",
                extra={
                    "request_id": request_id,
                    "path": info.filename,
                    "error_code": arch_info.error["code"],
                },
            )
        out.append({
            "title": info.title,
            "model_name": info.model_name,
            # basename only, never absolute path — matches /sdapi/v1/sd-models posture.
            "filename": _basename(info.name),
            "hash": info.hash,
            "sha256": info.sha256,
            "arch": arch_info.arch,
            "dtype": arch_info.dtype,
            "error": arch_info.error,
        })
    out.sort(key=lambda r: r["title"])
    return out


def summarize_arches(records: list[dict]) -> list[str]:
    """Sorted, unique arches present in ``records``, excluding labels
    that don't correspond to a selectable client mode."""
    return sorted({
        r["arch"] for r in records
        if r["arch"] not in _NON_MODE_ARCHES
    })


def attach(router: APIRouter) -> None:
    @router.get("/models/sd-checkpoints")
    def sd_checkpoints(request: Request) -> dict:
        records = enumerate_checkpoints(request)
        return {
            "checkpoints": records,
            "available_arches": summarize_arches(records),
        }


def register_capabilities() -> None:
    capabilities.register("models/disk-scan")
