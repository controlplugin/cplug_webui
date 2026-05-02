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

Race semantics: ``shared.state.interrupt()`` is *global* — it sets a flag
that the next sample-loop check honours regardless of which task is
running. If the running task changes between our identity check and the
interrupt call, we'd kill an unrelated job. We narrow that window by
re-checking ``progress.current_task == id_task`` immediately before the
interrupt call. A sub-microsecond residual race remains and is accepted
per spec §5.4 (cancel ops are idempotent; client retries are cheap).
"""

from __future__ import annotations

from fastapi import APIRouter, Path

from . import cancelled_tasks

# Upstream task IDs look like ``task(txt2img-XXXXXXX)`` (see
# ``modules/progress.py:create_task_id``); 7-char alphanumeric tail plus a
# short type prefix. 128 chars accommodates client-supplied
# ``force_task_id`` values comfortably while rejecting oversize blobs that
# would amplify log/registry storage.
_ID_TASK_REGEX = r"^[A-Za-z0-9_:.\-()]+$"
_ID_TASK_MAX = 128


def _classify_and_act(id_task: str) -> str:
    """Pop / interrupt / record. Returns the response state string."""
    from modules import progress, shared

    # Snapshot finished/cancelled membership BEFORE pop so the running
    # task completing between two reads cannot make us misclassify.
    already_finished_at_entry = id_task in progress.finished_tasks
    was_cancelled_at_entry = cancelled_tasks.has(id_task)

    was_queued = progress.pending_tasks.pop(id_task, None) is not None

    # Re-read current_task RIGHT BEFORE interrupting so we don't fire
    # a global interrupt against an unrelated job that started in the
    # window between any earlier reads and now.
    if progress.current_task == id_task:
        try:
            shared.state.interrupt()
        except Exception:
            # Don't fail cancel because interrupt errored — the
            # cancelled_tasks marker is still useful and the next
            # sample-loop check will pick up state.interrupted anyway.
            pass
        cancelled_tasks.add(id_task)
        return "cancelled"

    if was_queued:
        cancelled_tasks.add(id_task)
        return "cancelled"
    if was_cancelled_at_entry:
        return "already_cancelled"
    if already_finished_at_entry:
        return "already_completed"
    return "not_found"


def attach(router: APIRouter) -> None:
    @router.post("/session/cancel/{id_task}")
    def cancel(
        id_task: str = Path(..., max_length=_ID_TASK_MAX, pattern=_ID_TASK_REGEX),
    ) -> dict:
        return {"id_task": id_task, "state": _classify_and_act(id_task)}
