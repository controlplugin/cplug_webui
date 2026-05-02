"""``POST /cplugapi/v1/session/cancel/{id_task}`` — Track 05 §5.4.

Per-task cancellation. ``/sdapi/v1/interrupt`` is global and cannot drain
queued tasks; this endpoint:

1. pops the task from ``pending_tasks`` (no-op if absent)
2. fires ``shared.state.interrupt()`` if the task is currently running
3. records the cancellation in :mod:`cancelled_tasks` so late lookups
   return ``"already_cancelled"`` instead of ``"not_found"``

**Must NOT take ``queue_lock``.** Holding the lock would block behind the
running generation — the very thing we want to interrupt. ``pending_tasks``
mutation is already done outside the lock by upstream ``api.py``;
``shared.state.interrupt()`` is a cooperative flag setter.

Race semantics: the task may transition queued→running between our pop
attempt and our running-check. Belt-and-suspenders approach: pop AND fire
interrupt if ``current_task == id_task`` after the pop. Both ops are
idempotent.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import cancelled_tasks


def _classify_and_act(id_task: str) -> str:
    """Pop / interrupt / record. Returns the response state string."""
    from modules import progress, shared

    # NOTE: order matters. Snapshot finished_tasks first because the
    # running task could complete and land in finished_tasks between any
    # two reads below.
    already_finished_at_entry = id_task in progress.finished_tasks
    was_cancelled_at_entry = cancelled_tasks.has(id_task)

    was_queued = progress.pending_tasks.pop(id_task, None) is not None
    running = progress.current_task == id_task

    if running:
        try:
            shared.state.interrupt()
        except Exception:
            # Don't fail cancel because interrupt errored — the
            # cancelled_tasks marker is still useful and the next
            # sample-loop check will pick up state.interrupt anyway
            # if it reset to True elsewhere.
            pass

    if was_queued or running:
        cancelled_tasks.add(id_task)
        return "cancelled"
    if was_cancelled_at_entry:
        return "already_cancelled"
    if already_finished_at_entry:
        return "already_completed"
    return "not_found"


def attach(router: APIRouter) -> None:
    @router.post("/session/cancel/{id_task}")
    def cancel(id_task: str) -> dict:
        return {"id_task": id_task, "state": _classify_and_act(id_task)}
