"""``GET /cplugapi/v1/models/architectures`` — lightweight summary endpoint.

Same compute as ``/cplugapi/v1/models/sd-checkpoints`` but trims the
response to just ``available_arches`` so the ControlPlugin mode picker
can render without paying the full listing's wire cost on every refresh.

Excludes ``unknown`` and ``not_a_checkpoint`` from the result — those
labels never correspond to a selectable mode in the client UI.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from . import capabilities
from . import sd_checkpoints


def attach(router: APIRouter) -> None:
    @router.get("/models/architectures")
    def architectures(request: Request) -> dict:
        records = sd_checkpoints.enumerate_checkpoints(request)
        return {"available_arches": sd_checkpoints.summarize_arches(records)}


def register_capabilities() -> None:
    capabilities.register("models/architectures-available")
