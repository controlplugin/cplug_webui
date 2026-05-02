"""Tests for ``modules.cplugapi.queue_endpoint``."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, queue_endpoint, setup_cplugapi


def _make_client_with(extra_attach):
    """Mount cplugapi + queue_endpoint routes."""
    app = FastAPI()
    setup_cplugapi(app)
    extra = APIRouter()
    extra_attach(extra)
    app.include_router(extra, prefix=PREFIX)
    return TestClient(app)


def setup_function(_):
    """Wipe the EMA estimator between tests."""
    queue_endpoint.reset_estimator()


def test_empty_state_returns_empty_arrays(progress_stub, clean_capabilities):
    client = _make_client_with(queue_endpoint.attach)
    r = client.get(f"{PREFIX}/queue")
    assert r.status_code == 200
    assert r.json() == {"running": [], "pending": [], "history_recent": []}


def test_pending_tasks_surface(progress_stub, clean_capabilities):
    progress_stub.pending_tasks["task-A"] = 1000.5
    progress_stub.pending_tasks["task-B"] = 1001.7

    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    assert body["running"] == []
    assert body["history_recent"] == []
    pending = body["pending"]
    assert len(pending) == 2
    assert pending[0]["id_task"] == "task-A"
    assert pending[0]["submitted_at"] == 1000.5
    # No completions yet -> eta is null.
    assert pending[0]["eta_ms_p50"] is None


def test_current_task_becomes_running(progress_stub, clean_capabilities):
    progress_stub.current_task = "running-1"
    progress_stub.pending_tasks["queued-1"] = 42.0

    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    assert body["running"] == [{"id_task": "running-1", "started_at": None}]
    assert len(body["pending"]) == 1


def test_history_truncates_to_last_10(progress_stub, clean_capabilities):
    for i in range(25):
        progress_stub.finished_tasks.append(f"done-{i:02d}")

    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    history = body["history_recent"]
    assert len(history) == 10
    # Most recent 10 are the tail (done-15..done-24).
    ids = [h["id_task"] for h in history]
    assert ids == [f"done-{i:02d}" for i in range(15, 25)]
    # finished_at is null for upstream string entries.
    assert all(h["finished_at"] is None for h in history)


def test_history_with_dict_entries(progress_stub, clean_capabilities):
    """Forward-compat: if a future fork stores dicts, we surface fields."""
    progress_stub.finished_tasks.append({"id_task": "rich-1", "finished_at": 42.5})

    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    assert body["history_recent"] == [{"id_task": "rich-1", "finished_at": 42.5}]


def test_ema_populates_after_first_completion(progress_stub, clean_capabilities):
    queue_endpoint.record_completion_ms(2000.0)
    progress_stub.pending_tasks["next-1"] = 1.0

    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    eta = body["pending"][0]["eta_ms_p50"]
    assert eta == 2000.0


def test_ema_smooths_across_completions(progress_stub, clean_capabilities):
    """First completion seeds the EMA; subsequent values move it toward
    the new sample by alpha=0.2."""
    queue_endpoint.record_completion_ms(1000.0)  # seeds EMA
    queue_endpoint.record_completion_ms(2000.0)  # 0.2*2000 + 0.8*1000 = 1200
    progress_stub.pending_tasks["x"] = 0.0

    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    eta = body["pending"][0]["eta_ms_p50"]
    # Approximate — the smoothing factor is the contract, exact float is.
    assert 1199.99 <= eta <= 1200.01


def test_negative_durations_are_ignored(progress_stub, clean_capabilities):
    """A negative duration is nonsensical (clock skew); ignore it."""
    queue_endpoint.record_completion_ms(-5.0)
    progress_stub.pending_tasks["x"] = 0.0

    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    assert body["pending"][0]["eta_ms_p50"] is None


def test_ema_reasonable_under_repeated_samples(progress_stub, clean_capabilities):
    """Feeding the same value repeatedly should converge to that value."""
    for _ in range(50):
        queue_endpoint.record_completion_ms(750.0)
    est = queue_endpoint._estimator.estimate_ms()
    assert 749.0 <= est <= 751.0


def test_register_capabilities_adds_queue(clean_capabilities):
    from modules.cplugapi import capabilities

    queue_endpoint.register_capabilities()
    assert "queue" in capabilities.enabled_capabilities()


def test_running_when_no_current_task(progress_stub, clean_capabilities):
    progress_stub.current_task = None
    client = _make_client_with(queue_endpoint.attach)
    body = client.get(f"{PREFIX}/queue").json()
    assert body["running"] == []


def test_pending_tolerates_non_numeric_timestamp(progress_stub, clean_capabilities):
    """Defensive: a malformed timestamp must not 500 the endpoint."""
    progress_stub.pending_tasks["bad"] = "not-a-number"

    client = _make_client_with(queue_endpoint.attach)
    r = client.get(f"{PREFIX}/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["pending"][0]["submitted_at"] is None
