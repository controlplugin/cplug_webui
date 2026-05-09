"""Tests for ``POST /cplugapi/v1/session/preempt``.

Coverage:
- No-op when nothing is running (200, ``was_running=false``,
  ``preempted_task_id=null``).
- Cancels the running task: fires ``shared.state.interrupt()``, marks
  the task in ``cancelled_tasks``, returns its id.
- Pending-queue drain with ``?clear_pending=1`` empties the queue and
  reports the count.
- Idempotent: a follow-up preempt after a successful one is a no-op
  (current_task is already null).
- Auth gate (401 when ``--api-auth`` dependency rejects the caller).
- Endpoint registers in /health.capabilities.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, cancelled_tasks, setup_cplugapi


def _client(auth_dependency=None):
    app = FastAPI()
    setup_cplugapi(app, auth_dependency=auth_dependency)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_cancelled():
    cancelled_tasks.reset()
    yield
    cancelled_tasks.reset()


def test_noop_when_nothing_running(progress_stub, shared_stub, clean_capabilities):
    """No running task, no pending queue → 200, was_running=false,
    preempted_task_id=null."""
    r = _client().post(f"{PREFIX}/session/preempt")
    assert r.status_code == 200
    body = r.json()
    assert body["was_running"] is False
    assert body["preempted_task_id"] is None
    assert body["cleared_pending"] == 0
    assert shared_stub.state.interrupt_called == 0


def test_cancels_running_task(progress_stub, shared_stub, clean_capabilities):
    """Running task is interrupted and added to cancelled_tasks."""
    progress_stub.current_task = "task(txt2img-RUNNING)"

    r = _client().post(f"{PREFIX}/session/preempt")
    assert r.status_code == 200
    body = r.json()
    assert body["was_running"] is True
    assert body["preempted_task_id"] == "task(txt2img-RUNNING)"
    assert body["cleared_pending"] == 0
    # interrupt() fired exactly once on the global state.
    assert shared_stub.state.interrupt_called == 1
    # Late status pokes on the cancelled id should now report
    # already_cancelled rather than not_found.
    assert cancelled_tasks.has("task(txt2img-RUNNING)")


def test_clear_pending_drains_queue(progress_stub, shared_stub, clean_capabilities):
    """``?clear_pending=1`` pops every entry from pending_tasks and
    records each in the cancelled registry."""
    progress_stub.current_task = None
    progress_stub.pending_tasks["task(a)"] = 1.0
    progress_stub.pending_tasks["task(b)"] = 2.0
    progress_stub.pending_tasks["task(c)"] = 3.0

    r = _client().post(f"{PREFIX}/session/preempt?clear_pending=1")
    assert r.status_code == 200
    body = r.json()
    assert body["was_running"] is False
    assert body["cleared_pending"] == 3
    assert len(progress_stub.pending_tasks) == 0
    # Each pending task got recorded so /sdapi/v1/progress polling on
    # any of them returns already_cancelled.
    for tid in ("task(a)", "task(b)", "task(c)"):
        assert cancelled_tasks.has(tid)


def test_clear_pending_default_off(progress_stub, shared_stub, clean_capabilities):
    """Without ``?clear_pending=1`` the queue is left alone — operator
    must explicitly opt in to backlog drain."""
    progress_stub.pending_tasks["task(stuck)"] = 0.0

    r = _client().post(f"{PREFIX}/session/preempt")
    body = r.json()
    assert body["cleared_pending"] == 0
    assert "task(stuck)" in progress_stub.pending_tasks


def test_running_plus_pending_with_clear(progress_stub, shared_stub, clean_capabilities):
    """Combined case: running task is killed AND pending queue drained."""
    progress_stub.current_task = "task(running)"
    progress_stub.pending_tasks["task(p1)"] = 1.0
    progress_stub.pending_tasks["task(p2)"] = 2.0

    r = _client().post(f"{PREFIX}/session/preempt?clear_pending=1")
    body = r.json()
    assert body["preempted_task_id"] == "task(running)"
    assert body["was_running"] is True
    assert body["cleared_pending"] == 2
    assert shared_stub.state.interrupt_called == 1


def test_idempotent_double_call(progress_stub, shared_stub, clean_capabilities):
    """Calling preempt twice in a row: first does the work, second is
    a no-op since current_task got cleared by an upstream consumer.
    Test simulates the upstream clearing it post-interrupt."""
    progress_stub.current_task = "task(once)"

    client = _client()
    r1 = client.post(f"{PREFIX}/session/preempt")
    assert r1.json()["was_running"] is True

    # Simulate upstream sample loop noticing interrupt and exiting,
    # which clears current_task.
    progress_stub.current_task = None

    r2 = client.post(f"{PREFIX}/session/preempt")
    body = r2.json()
    assert body["was_running"] is False
    assert body["preempted_task_id"] is None
    # interrupt() shouldn't fire a second time.
    assert shared_stub.state.interrupt_called == 1


def test_auth_required_returns_401(progress_stub, clean_capabilities):
    def reject_all():
        raise HTTPException(status_code=401, detail="nope")

    r = _client(auth_dependency=reject_all).post(f"{PREFIX}/session/preempt")
    assert r.status_code == 401


def test_capability_registered(progress_stub, clean_capabilities):
    caps = _client().get(f"{PREFIX}/health").json()["capabilities"]
    assert "session/preempt" in caps
