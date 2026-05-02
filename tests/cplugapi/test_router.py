"""Endpoint smoke tests for ``/cplugapi/v1/*`` via FastAPI TestClient."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import (
    FORK_NAME,
    FORK_VERSION,
    PREFIX,
    UPSTREAM_NAME,
    setup_cplugapi,
)


def _make_client(auth_dependency=None):
    app = FastAPI()
    setup_cplugapi(app, auth_dependency=auth_dependency)
    return TestClient(app)


def test_ping_returns_ok(clean_capabilities):
    client = _make_client()
    r = client.get(f"{PREFIX}/_ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_identify_returns_constants(clean_capabilities):
    client = _make_client()
    r = client.get(f"{PREFIX}/identify")
    assert r.status_code == 200
    body = r.json()
    assert body["fork"] == FORK_NAME
    assert body["fork_version"] == FORK_VERSION
    assert body["upstream"] == UPSTREAM_NAME
    # commits default to "unknown" without CI env vars.
    assert "fork_commit" in body
    assert "upstream_commit" in body


def test_identify_is_unauthenticated_even_when_auth_set(clean_capabilities):
    """Per Track 05 §5.1: identify must work without credentials."""

    def reject_all():
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="nope")

    client = _make_client(auth_dependency=reject_all)
    r = client.get(f"{PREFIX}/identify")
    assert r.status_code == 200


def test_health_lists_capabilities(progress_stub, clean_capabilities):
    client = _make_client()
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["queue_depth"] == 0
    assert body["active_task_id"] is None
    assert "identify" in body["capabilities"]
    assert "session/cancel" in body["capabilities"]


def test_health_busy_when_queue_deep(progress_stub, clean_capabilities):
    for i in range(5):
        progress_stub.pending_tasks[f"task-{i}"] = float(i)
    client = _make_client()
    r = client.get(f"{PREFIX}/health")
    body = r.json()
    assert body["status"] == "busy"
    assert body["queue_depth"] == 5


def test_health_active_task_surfaced(progress_stub, clean_capabilities):
    progress_stub.current_task = "stroke-42"
    client = _make_client()
    body = client.get(f"{PREFIX}/health").json()
    assert body["active_task_id"] == "stroke-42"


def test_health_detailed_does_not_500(progress_stub, clean_capabilities):
    """Detailed mode pulls best-effort diagnostics; absent torch/cuda
    must not raise."""
    client = _make_client()
    r = client.get(f"{PREFIX}/health?detailed=true")
    assert r.status_code == 200
    body = r.json()
    # Forward-compat keys MUST be present (null/empty when unimplemented).
    assert "vram_used_mb" in body
    assert "warm_pool_slots" in body
    assert "active_attention_backend" in body
    assert "comfy_finalization_tax_active" in body


def test_version_returns_fork_constants(clean_capabilities):
    from modules.cplugapi import version_endpoint

    version_endpoint.reset()
    client = _make_client()
    r = client.get(f"{PREFIX}/version")
    assert r.status_code == 200
    body = r.json()
    assert body["fork"] == FORK_NAME
    assert body["upstream_branch"] == "neo"
    assert "python_version" in body
    assert "platform" in body
    # Forward-compat keys must be present even when their data sources
    # haven't shipped (Phase 3/5/6).
    assert "loaded_extensions" in body
    assert "attention_backend" in body
    assert "active_quantization" in body


def test_version_is_cached(clean_capabilities):
    """A second hit returns the same payload because of the 60 s cache."""
    from modules.cplugapi import version_endpoint

    version_endpoint.reset()
    client = _make_client()
    a = client.get(f"{PREFIX}/version").json()
    b = client.get(f"{PREFIX}/version").json()
    assert a["fork_build_date"] == b["fork_build_date"]


def test_version_cache_returns_independent_dicts(clean_capabilities):
    """Mutating one cached payload must not poison the next caller."""
    from modules.cplugapi import version_endpoint

    version_endpoint.reset()
    a = version_endpoint._cache.get(version_endpoint._build_payload)
    a["fork"] = "MUTATED"
    b = version_endpoint._cache.get(version_endpoint._build_payload)
    assert b["fork"] != "MUTATED"


def test_auth_dependency_protects_private_routes_only(clean_capabilities):
    """When auth rejects, /identify still works but /health does not."""
    from fastapi import HTTPException

    def reject_all():
        raise HTTPException(status_code=401, detail="nope")

    client = _make_client(auth_dependency=reject_all)
    assert client.get(f"{PREFIX}/identify").status_code == 200
    assert client.get(f"{PREFIX}/health").status_code == 401
    assert client.get(f"{PREFIX}/version").status_code == 401
    assert client.get(f"{PREFIX}/_ping").status_code == 401
