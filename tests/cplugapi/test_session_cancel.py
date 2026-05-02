"""Functional tests for ``POST /cplugapi/v1/session/cancel/{id_task}``."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, setup_cplugapi


def _client():
    app = FastAPI()
    setup_cplugapi(app)
    return TestClient(app)


def test_cancel_queued_task_pops_and_marks(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    progress_stub.pending_tasks["task-A"] = 1.0
    progress_stub.pending_tasks["task-B"] = 2.0

    body = _client().post(f"{PREFIX}/session/cancel/task-A").json()
    assert body == {"id_task": "task-A", "state": "cancelled"}

    # Task A should be gone from pending; task B untouched.
    assert "task-A" not in progress_stub.pending_tasks
    assert "task-B" in progress_stub.pending_tasks
    # Interrupt was NOT fired (task wasn't running).
    assert shared_stub.state.interrupt_called == 0


def test_cancel_running_task_fires_interrupt(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    progress_stub.current_task = "running-task"

    body = _client().post(f"{PREFIX}/session/cancel/running-task").json()
    assert body == {"id_task": "running-task", "state": "cancelled"}
    assert shared_stub.state.interrupt_called == 1


def test_cancel_finished_task_returns_already_completed(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    progress_stub.finished_tasks.append("done-task")

    body = _client().post(f"{PREFIX}/session/cancel/done-task").json()
    assert body == {"id_task": "done-task", "state": "already_completed"}
    assert shared_stub.state.interrupt_called == 0


def test_cancel_already_cancelled_returns_already_cancelled(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    from modules.cplugapi import cancelled_tasks

    cancelled_tasks.add("prev-task")

    body = _client().post(f"{PREFIX}/session/cancel/prev-task").json()
    assert body == {"id_task": "prev-task", "state": "already_cancelled"}
    assert shared_stub.state.interrupt_called == 0


def test_cancel_unknown_task_returns_not_found(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    body = _client().post(f"{PREFIX}/session/cancel/never-existed").json()
    assert body == {"id_task": "never-existed", "state": "not_found"}
    assert shared_stub.state.interrupt_called == 0


def test_cancel_returns_200_for_unknown_task_not_404(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    """Per §5.4: unknown id returns 200 with state='not_found' so the client
    can distinguish protocol errors from logical states."""
    r = _client().post(f"{PREFIX}/session/cancel/never-existed")
    assert r.status_code == 200


def test_belt_and_suspenders_queued_to_running_race(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    """Task starts queued, transitions to running between the pop attempt
    and the running-check. We must still fire interrupt + mark cancelled."""

    progress_stub.pending_tasks["racy-task"] = 1.0

    # Simulate the race: pop succeeds (queued path), then current_task gets
    # set to the same id (transition fired between pop and current-task read).
    # In the wild this happens because api.py's queue worker pops then
    # assigns current_task; we test by setting both before the call.
    progress_stub.current_task = "racy-task"

    body = _client().post(f"{PREFIX}/session/cancel/racy-task").json()
    assert body["state"] == "cancelled"
    # Both code paths fired: queued-pop AND running-interrupt.
    assert "racy-task" not in progress_stub.pending_tasks
    assert shared_stub.state.interrupt_called == 1


def test_interrupt_failure_does_not_break_response(progress_stub, shared_stub, clean_cancelled, clean_capabilities):
    """If shared.state.interrupt() raises, cancel still returns 200 with
    state='cancelled' and the cancellation is recorded."""

    def boom():
        raise RuntimeError("interrupt mechanism failed")

    progress_stub.current_task = "running"
    shared_stub.state.interrupt = boom  # type: ignore[assignment]

    body = _client().post(f"{PREFIX}/session/cancel/running").json()
    assert body == {"id_task": "running", "state": "cancelled"}
