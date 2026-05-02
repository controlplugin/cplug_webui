"""``GET /cplugapi/v1/queue`` — task queue introspection.

Surfaces ``modules.progress`` state as a structured payload the desktop
client can render. Three buckets:

* ``running``         — at most one entry: the ``current_task``.
* ``pending``         — everything in ``pending_tasks`` (oldest first),
                        each entry annotated with ``eta_ms_p50``
                        (rolling-EMA estimate, ``null`` until we have
                        enough history).
* ``history_recent``  — last 10 entries from ``finished_tasks``.

The module is named ``queue_endpoint.py`` (not ``queue.py``) on
purpose — ``queue`` shadows the stdlib module and breaks any code that
tries to ``import queue`` from inside the package directory.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from typing import Any, Optional

from fastapi import APIRouter

from . import capabilities

# Cap on how much history we expose. The Rust client renders a small
# scrolling list; bigger windows just bloat the response.
_HISTORY_RECENT = 10

# Rolling completion-duration window for the EMA. 32 samples is enough
# to smooth single-frame outliers without taking minutes to react when
# the artist switches sampler / resolution.
_EMA_WINDOW = 32
# Smoothing factor for the EMA. 0.2 == "this completion is 20% of the
# new estimate; 80% inherited from the previous estimate". Picked by
# eyeballing the live-sketching cadence (1-3 s strokes).
_EMA_ALPHA = 0.2


class _Estimator:
    """Tracks completion durations + maintains a smoothed ETA estimate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: deque[float] = deque(maxlen=_EMA_WINDOW)
        # EMA of completion duration in milliseconds. ``None`` until we
        # see the first completion.
        self._ema_ms: Optional[float] = None

    def record(self, duration_ms: float) -> None:
        """Feed a new completion duration into the estimator."""
        if duration_ms < 0:
            return
        with self._lock:
            self._samples.append(duration_ms)
            if self._ema_ms is None:
                self._ema_ms = duration_ms
            else:
                self._ema_ms = (
                    _EMA_ALPHA * duration_ms + (1.0 - _EMA_ALPHA) * self._ema_ms
                )

    def estimate_ms(self) -> Optional[float]:
        with self._lock:
            return self._ema_ms

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._ema_ms = None


_estimator = _Estimator()


def record_completion_ms(duration_ms: float) -> None:
    """Public hook so other modules can feed completion timings in."""
    _estimator.record(duration_ms)


def reset_estimator() -> None:
    """Test-only: forget all recorded completions."""
    _estimator.reset()


def _build_running(progress_mod) -> list[dict[str, Any]]:
    current = getattr(progress_mod, "current_task", None)
    if not current:
        return []
    started_at = _running_started_at(progress_mod, current)
    return [{"id_task": current, "started_at": started_at}]


def _running_started_at(progress_mod, current_task: str) -> Optional[float]:
    """Best-effort: upstream ``start_task`` does not record a timestamp,
    so the only signal we have is the most-recent moment we observed
    ``current_task`` change. We expose ``None`` until something fork-side
    plumbs a wallclock through (Track 05 Phase 3 work)."""
    return None


def _build_pending(progress_mod) -> list[dict[str, Any]]:
    pending = getattr(progress_mod, "pending_tasks", None)
    if not pending:
        return []
    eta = _estimator.estimate_ms()
    out: list[dict[str, Any]] = []
    # ``pending_tasks`` is documented as an OrderedDict of ``id -> ts``;
    # tolerate sets / lists / mappings of other shapes defensively.
    items: list[tuple[str, Any]]
    if isinstance(pending, OrderedDict) or isinstance(pending, dict):
        items = list(pending.items())
    else:
        items = [(str(x), None) for x in pending]
    for id_task, submitted_at in items:
        out.append(
            {
                "id_task": id_task,
                "submitted_at": _coerce_timestamp(submitted_at),
                "eta_ms_p50": eta,
            }
        )
    return out


def _coerce_timestamp(value: Any) -> Optional[float]:
    """Convert mixed timestamp types to a float / None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_history(progress_mod) -> list[dict[str, Any]]:
    finished = getattr(progress_mod, "finished_tasks", None)
    if not finished:
        return []
    # ``finished_tasks`` is upstream a list of strings; some forks /
    # future code may store dicts. Handle both shapes.
    tail = list(finished)[-_HISTORY_RECENT:]
    out: list[dict[str, Any]] = []
    for entry in tail:
        if isinstance(entry, dict):
            out.append(
                {
                    "id_task": entry.get("id_task") or entry.get("id") or "",
                    "finished_at": _coerce_timestamp(entry.get("finished_at")),
                }
            )
        else:
            out.append({"id_task": str(entry), "finished_at": None})
    return out


def attach(router: APIRouter) -> None:
    @router.get("/queue")
    def queue() -> dict:
        # Local import — ``modules.progress`` may not exist in degenerate
        # boot environments (e.g. unit tests before the stub fixture
        # lands).
        try:
            from modules import progress as progress_mod
        except Exception:
            return {"running": [], "pending": [], "history_recent": []}

        return {
            "running": _build_running(progress_mod),
            "pending": _build_pending(progress_mod),
            "history_recent": _build_history(progress_mod),
        }


def register_capabilities() -> None:
    """Advertise queue introspection. Idempotent."""
    capabilities.register("queue")
