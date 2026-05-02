"""T27 — cancel race-condition stress test.

Acceptance criterion (Track 05 §5.4): 1000 iterations, no flake, no
leaked tasks, no crashes. We exercise the cancellation logic directly
(``_classify_and_act``) rather than through TestClient — the race we're
hunting is in the queued→running→finished state mutation, not in HTTP.

A separate end-to-end test (``test_session_cancel.py``) covers the
HTTP path.
"""

from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor

from modules.cplugapi import cancelled_tasks
from modules.cplugapi.session_cancel import _classify_and_act

ITERATIONS = 1000
WORKERS = 8
TASK_POOL_SIZE = 10


def _life_cycler(progress_stub, stop_flag):
    """Cycle tasks through queued → running → finished, mimicking the
    api.py queue worker pattern."""
    rng = random.Random(0xC0FFEE)
    while not stop_flag.is_set():
        task_id = f"task-{rng.randint(0, TASK_POOL_SIZE - 1)}"
        progress_stub.pending_tasks[task_id] = rng.random()
        if rng.random() < 0.5:
            progress_stub.pending_tasks.pop(task_id, None)
            progress_stub.current_task = task_id
            progress_stub.current_task = None
            progress_stub.finished_tasks.append(task_id)
            if len(progress_stub.finished_tasks) > 16:
                progress_stub.finished_tasks.pop(0)


def test_no_crash_under_concurrent_cancellation(
    progress_stub, shared_stub, clean_cancelled, clean_capabilities
):
    stop_flag = threading.Event()
    cycler = threading.Thread(
        target=_life_cycler, args=(progress_stub, stop_flag), daemon=True
    )
    cycler.start()

    rng = random.Random(0xBEEF)
    valid_states = {"cancelled", "already_cancelled", "already_completed", "not_found"}
    errors: list[str] = []

    def fire(_):
        task_id = f"task-{rng.randint(0, TASK_POOL_SIZE - 1)}"
        try:
            state = _classify_and_act(task_id)
            if state not in valid_states:
                errors.append(f"unexpected state={state}")
        except Exception as e:
            errors.append(f"exception={e!r}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(fire, range(ITERATIONS)))

    stop_flag.set()
    cycler.join(timeout=5.0)

    assert errors == [], (
        f"{len(errors)} errors out of {ITERATIONS} iterations: {errors[:5]}"
    )
    # The cancelled-tasks registry must respect its hard cap even under
    # heavy concurrent add pressure.
    assert cancelled_tasks.size() <= cancelled_tasks._MAX_ENTRIES
