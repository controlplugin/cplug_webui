"""``POST /cplugapi/v1/session/preempt`` — cancel whatever is running.

The desktop ControlPlugin client uses a sketch workflow where each
stroke fires a new generation. By the time stroke N+1 arrives, the
gen for stroke N is stale — the artist doesn't want to wait for it.
Today the client must already know stroke N's task ID to call
``/session/cancel/{id_task}``; this endpoint adds the
"cancel whatever is running, I don't care which" primitive so the
client can pipeline ``preempt → submit`` without bookkeeping.

Contract::

    POST /cplugapi/v1/session/preempt
    POST /cplugapi/v1/session/preempt?clear_pending=1

    -> 200
    {
      "preempted_task_id": "task(txt2img-XXXXXXX)" | null,
      "was_running": true | false,
      "cleared_pending": <int>
    }

- ``preempted_task_id`` — the id we just interrupted, or ``null`` if
  nothing was running. Diagnostic, not load-bearing.
- ``was_running`` — true iff ``progress.current_task`` was non-null
  at entry. Distinguishes "we cancelled a real task" from "no-op".
- ``cleared_pending`` — number of queued tasks dropped. ``0`` unless
  ``?clear_pending=1`` was passed. Zero pending → ``0``; this is the
  user's drain-the-backlog escape hatch when sketch sessions stack
  up multiple gens.

Race + correctness notes (mirrored from :mod:`session_cancel`):

- **Must NOT take queue_lock.** Holding it would deadlock behind the
  generation we're trying to cancel — the very thing we want to
  interrupt.
- ``shared.state.interrupt()`` is global: it sets a flag the next
  sample-loop check honours. We re-read ``progress.current_task``
  immediately before the interrupt call to narrow the window where
  the running task could change between observation and action. A
  sub-microsecond residual race remains and is accepted (cancel is
  idempotent; client retries cost nothing).

Recommended client pattern::

    # fire-and-forget the preempt; immediately submit the new gen
    let _ = http.post("/cplugapi/v1/session/preempt").await;
    let img = http.post("/sdapi/v1/txt2img", payload).await?;

The new gen blocks on Forge's ``queue_lock`` for ~1 sample step
(cancelled gen has to notice ``state.interrupted`` and exit). That
latency is intrinsic to cooperative cancellation; no API shape
removes it.
"""

from __future__ import annotations


from fastapi import APIRouter, Query

from . import cancelled_tasks


def _preempt(clear_pending: bool) -> dict:
    """Pop / interrupt / record. Returns the response dict.

    Two-stage:

    1. Interrupt the running task (if any). ``shared.state.interrupt()``
       sets the cooperative flag; the sampler exits at its next check.
       Marker added to ``cancelled_tasks`` so late status pokes return
       ``"already_cancelled"`` instead of ``"not_found"``.
    2. Drain the pending queue when ``clear_pending=True``. Pop each
       entry, record in ``cancelled_tasks``. Snapshot the keys before
       iterating so a concurrent submit doesn't trip
       ``RuntimeError: dictionary changed size during iteration``.
    """
    from modules import progress, shared

    # Snapshot current_task BEFORE we attempt anything else — by the
    # time we record was_running we want to reflect the state at entry,
    # not after the interrupt has had time to propagate.
    current_at_entry = progress.current_task
    was_running = current_at_entry is not None

    if was_running:
        # Re-read RIGHT before interrupting — the running task could
        # have changed (rare but legal) between current_at_entry and
        # the interrupt call. If it's still our snapshot, kill it.
        if progress.current_task == current_at_entry:
            try:
                shared.state.interrupt()
            except Exception:
                # Don't fail preempt because interrupt errored — the
                # cancelled_tasks marker is still useful and the next
                # sample-loop check will pick up state.interrupted
                # if it was set.
                pass
            cancelled_tasks.add(current_at_entry)

    cleared = 0
    if clear_pending:
        # Snapshot keys to avoid RuntimeError during iteration if a
        # concurrent ``/sdapi/v1/txt2img`` submits while we drain.
        # ``progress.pending_tasks`` is documented as an OrderedDict
        # of id -> timestamp.
        pending_keys = list(progress.pending_tasks.keys())
        for id_task in pending_keys:
            if progress.pending_tasks.pop(id_task, None) is not None:
                cancelled_tasks.add(id_task)
                cleared += 1

    return {
        "preempted_task_id": current_at_entry if was_running else None,
        "was_running": was_running,
        "cleared_pending": cleared,
    }


def attach(router: APIRouter) -> None:
    @router.post("/session/preempt")
    def preempt(
        clear_pending: bool = Query(
            False,
            description=(
                "Also drain the pending queue. Default false (only the "
                "running task is cancelled). Pass 1/true to clear queued "
                "gens too — escape hatch for stuck-backlog scenarios."
            ),
        ),
    ) -> dict:
        return _preempt(clear_pending)


def register_capabilities() -> None:
    from . import capabilities

    capabilities.register("session/preempt")
