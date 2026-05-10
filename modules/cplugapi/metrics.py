"""Prometheus / OpenMetrics endpoint for ``/cplugapi/v1/*``.

Vendored, hand-rolled exposition (text/plain; version=0.0.4) so the
fork stays free of a hard ``prometheus_client`` dependency. The
formatter is intentionally minimal — the metric set is small, the
output is well-specified, and the cardinality/escaping pitfalls a
real client would solve for free are addressed inline below.

Metrics exposed (all namespaced ``cplugapi_*``):

* ``cplugapi_requests_total{method, path, status}`` — counter.
* ``cplugapi_request_duration_seconds{method, path}`` — histogram
  with fixed buckets ``[5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms,
  1s, 2.5s, 5s, 10s, +Inf]``. Bucket counts are cumulative as the
  Prometheus exposition format requires.
* ``cplugapi_idempotency_replays_total`` — counter, incremented from
  the access-log handler whenever an ``Idempotency-Replayed: true``
  header is seen on the response.
* ``cplugapi_active_task_id_present`` — gauge, sampled at scrape
  time from ``modules.progress.current_task`` (1 when non-null).

Integration with the access-log path is **non-invasive**: a
:class:`logging.Handler` attached to the ``cplugapi.access`` logger
reads ``record.method``, ``record.path``, ``record.status``,
``record.dur_ms``, and ``record.replayed`` off the structured ``extra``
that :mod:`access_log` already populates. ``access_log.py`` is not
touched, so the integration survives upstream rebases that touch the
middleware layer.

**Cardinality control.** Path label values are normalised through
:func:`_normalise_path` — ``/cplugapi/v1/session/cancel/task(txt2img-…)``
collapses to ``/cplugapi/v1/session/cancel/{id_task}`` so a malicious
caller cannot blow out memory by hitting unique paths. As a
defence-in-depth measure, the registry caps distinct ``(method, path,
status)`` combinations at :data:`_CARDINALITY_CAP` and bins the
overflow into a synthetic ``path="<other>"`` bucket.

**Auth posture.** The endpoint mounts on the cplugapi *private* router
by default (inherits ``--api-auth``). Setting ``CPLUG_METRICS_PUBLIC=1``
flips :func:`is_public` to ``True`` so the wiring code in
``router.py`` can mount it on the public router instead — the typical
cloud pattern of scraping over a localhost / sidecar port without
credentials.

Hooks for future work (W8 rate limit, W12 graceful shutdown) live as
TODO comments next to the registry; those modules will call
``observe_*`` helpers on this module when they land. No hard
dependency in either direction today.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import Response

from . import capabilities

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
"""Prometheus text exposition format 0.0.4 media type."""

# Histogram buckets in seconds. Order is monotonically increasing; ``+Inf``
# is appended automatically by the formatter. The set is the same one
# every Prometheus tutorial / `prometheus_client.Histogram` default uses,
# minus the unused micro-buckets — desktop-loopback gens are >100ms
# floor, but health/queue/identify polls land in the sub-50ms region
# and we want shape there.
_BUCKETS_SECONDS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

# Cap on the number of distinct ``(method, path, status)`` combinations
# we track. Above this, new combinations spill into ``path="<other>"``.
# 100 is generous: the cplugapi surface has ~15 routes × ~5 status codes
# = ~75 combinations; the cap leaves headroom for templated paths
# without giving an attacker arbitrary memory growth.
_CARDINALITY_CAP = 100

# Sentinel path label for combinations beyond the cap.
_OVERFLOW_PATH = "<other>"

# Env var: setting this to 1/true/yes/on flips the metrics endpoint
# onto the public router. Default is private (auth-gated).
_ENV_PUBLIC = "CPLUG_METRICS_PUBLIC"


# ---------------------------------------------------------------------------
# Path normaliser
# ---------------------------------------------------------------------------
#
# The fork has a small, fixed surface — we can map known route patterns
# to their template form via prefix match. Anything unknown is passed
# through untouched (and bumps cardinality if novel).

_PREFIX = "/cplugapi/v1"

# Routes whose path includes a templated trailing parameter. Order
# matters only insofar as we strip the longest prefix wins; the table
# is short enough that linear scan is fine.
_TEMPLATED_PREFIXES: tuple[tuple[str, str], ...] = (
    (f"{_PREFIX}/session/cancel/", f"{_PREFIX}/session/cancel/{{id_task}}"),
    (f"{_PREFIX}/forge/preset/", f"{_PREFIX}/forge/preset/{{name}}"),
)


def _normalise_path(path: str) -> str:
    """Map a concrete request path to a template-form path label.

    Examples
    --------
    >>> _normalise_path("/cplugapi/v1/session/cancel/task(txt2img-ABC)")
    '/cplugapi/v1/session/cancel/{id_task}'
    >>> _normalise_path("/cplugapi/v1/forge/preset/default")
    '/cplugapi/v1/forge/preset/{name}'
    >>> _normalise_path("/cplugapi/v1/health")
    '/cplugapi/v1/health'

    Untemplated cplugapi paths and any non-cplugapi paths (the access-
    log middleware is path-scoped, so we should never see these in
    practice) pass through verbatim.
    """
    for prefix, template in _TEMPLATED_PREFIXES:
        if path.startswith(prefix) and len(path) > len(prefix):
            return template
    return path


# ---------------------------------------------------------------------------
# Label escaping (Prometheus text exposition spec)
# ---------------------------------------------------------------------------
#
# Per the spec, label *values* must escape:
#   ``\``    → ``\\``
#   ``"``    → ``\"``
#   newline  → ``\n``
# Label *names* are unescaped — they are constrained by regex
# ``[a-zA-Z_][a-zA-Z0-9_]*`` and we control them at the source.


def _escape_label_value(value: str) -> str:
    """Escape a label value per Prometheus text exposition rules."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    """Render ``{name="value",...}`` for the given label sequence.

    Returns an empty string for an empty label set so the metric line
    becomes ``metric_name 42`` instead of ``metric_name{} 42`` — both
    are valid per the spec, but the bare form is what Prometheus's
    own client emits and it's cleaner output for human eyeballing.
    """
    if not labels:
        return ""
    parts = ",".join(
        f'{name}="{_escape_label_value(value)}"' for (name, value) in labels
    )
    return "{" + parts + "}"


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------


class _Counter:
    """Map of label-tuple → integer count.

    Label tuples are ``((name1, value1), (name2, value2), ...)``,
    sorted by name to canonicalise so equal-but-differently-ordered
    labels collapse to the same series.
    """

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: dict[tuple[tuple[str, str], ...], int] = {}
        self._lock = threading.Lock()

    def inc(self, labels: dict[str, str], amount: int = 1) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def snapshot(self) -> list[tuple[tuple[tuple[str, str], ...], int]]:
        with self._lock:
            return list(self._values.items())

    def cardinality(self) -> int:
        with self._lock:
            return len(self._values)


class _ScalarCounter:
    """Counter with no labels — single integer."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._value = 0
        self._lock = threading.Lock()

    def inc(self, amount: int = 1) -> None:
        with self._lock:
            self._value += amount

    def value(self) -> int:
        with self._lock:
            return self._value


class _Histogram:
    """Cumulative-bucket histogram with fixed buckets.

    For each label-tuple key, stores ``[count_per_bucket..., +Inf_count, sum]``
    where ``count_per_bucket[i]`` is the count of observations ``≤ buckets[i]``
    (cumulative — already in Prometheus exposition form).
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...] = _BUCKETS_SECONDS,
    ) -> None:
        self.name = name
        self.help = help_text
        self.buckets = buckets
        # Per-series state: list of cumulative bucket counts (length
        # ``len(buckets) + 1``; last slot is ``+Inf``) and a running sum.
        self._values: dict[tuple[tuple[str, str], ...], list[float]] = {}
        self._lock = threading.Lock()

    def observe(self, labels: dict[str, str], value: float) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                # ``len(buckets)`` cumulative slots + 1 +Inf slot + 1 sum.
                entry = [0.0] * (len(self.buckets) + 2)
                self._values[key] = entry
            # Find the lowest bucket whose upper bound covers ``value``,
            # then bump that bucket and every larger one (cumulative
            # semantics; +Inf is always bumped so it acts as the count).
            bumped = False
            for i, ub in enumerate(self.buckets):
                if value <= ub:
                    for j in range(i, len(self.buckets)):
                        entry[j] += 1
                    bumped = True
                    break
            # +Inf is the total observation count regardless.
            entry[len(self.buckets)] += 1
            # If the value exceeds every finite bucket, only +Inf was
            # bumped above; ``bumped`` stays False and that's correct.
            del bumped
            entry[len(self.buckets) + 1] += float(value)

    def snapshot(
        self,
    ) -> list[tuple[tuple[tuple[str, str], ...], list[float]]]:
        with self._lock:
            # Deep-ish copy so the caller can iterate without holding
            # the lock; the inner list is intentionally re-allocated.
            return [(k, list(v)) for k, v in self._values.items()]

    def cardinality(self) -> int:
        with self._lock:
            return len(self._values)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class _Registry:
    """Holds every metric we expose. One global instance.

    Test-only :func:`reset` clears every counter/histogram so tests can
    exercise the registry deterministically.
    """

    def __init__(self) -> None:
        self.requests_total = _Counter(
            "cplugapi_requests_total",
            "Total HTTP requests handled by /cplugapi/v1/*.",
        )
        self.request_duration = _Histogram(
            "cplugapi_request_duration_seconds",
            "HTTP request latency for /cplugapi/v1/* endpoints (seconds).",
        )
        self.idempotency_replays_total = _ScalarCounter(
            "cplugapi_idempotency_replays_total",
            "Total idempotency-cache replays served on /cplugapi/v1/*.",
        )
        # W8 hook — when the rate-limiter lands, register a counter
        # here named ``cplugapi_rate_limited_total{class}`` and increment
        # from the limiter middleware. Left as a placeholder rather than
        # registered now so the metric doesn't appear with a stuck zero.
        # W12 hook — when graceful shutdown lands, expose a gauge
        # ``cplugapi_draining`` reading from livez_readyz.is_draining(),
        # alongside the active-task gauge below.

    def reset(self) -> None:
        """Clear every metric. Test-only."""
        self.requests_total = _Counter(
            self.requests_total.name, self.requests_total.help
        )
        self.request_duration = _Histogram(
            self.request_duration.name,
            self.request_duration.help,
            self.request_duration.buckets,
        )
        self.idempotency_replays_total = _ScalarCounter(
            self.idempotency_replays_total.name,
            self.idempotency_replays_total.help,
        )

    # -- observation helpers ------------------------------------------------

    def record_request(
        self,
        method: str,
        path: str,
        status: int,
        dur_ms: Optional[float],
    ) -> None:
        """Record one observed request from the access-log path.

        ``dur_ms`` may be ``None`` if the access-log layer didn't capture
        timing (defensive — today it always does); the duration histogram
        is skipped in that case but the requests_total counter still
        increments so totals stay consistent.
        """
        path_label = self._capped_path(method, path, str(status))
        labels_full = {
            "method": method,
            "path": path_label,
            "status": str(status),
        }
        self.requests_total.inc(labels_full)
        if dur_ms is not None:
            self.request_duration.observe(
                {"method": method, "path": path_label},
                float(dur_ms) / 1000.0,
            )

    def _capped_path(self, method: str, path: str, status: str) -> str:
        """Apply normalisation + cardinality cap.

        If we have already seen :data:`_CARDINALITY_CAP` distinct
        ``(method, path, status)`` combinations and the incoming one
        is novel, bin it under :data:`_OVERFLOW_PATH`. Existing
        combinations always pass through (so an attacker spamming
        novel paths can't displace legitimate series).
        """
        normalised = _normalise_path(path)
        candidate = (
            ("method", method),
            ("path", normalised),
            ("status", status),
        )
        # Snapshot is racy with the increment in inc(), but at worst
        # we accept ``_CARDINALITY_CAP + N`` series for some small N
        # under high contention — the cap is defence-in-depth, not a
        # hard ceiling.
        existing = self.requests_total.snapshot()
        if any(k == candidate for k, _ in existing):
            return normalised
        if len(existing) >= _CARDINALITY_CAP:
            return _OVERFLOW_PATH
        return normalised


_registry = _Registry()


# ---------------------------------------------------------------------------
# Public observation API
# ---------------------------------------------------------------------------


def observe_request(
    method: str,
    path: str,
    status: int,
    dur_ms: Optional[float],
) -> None:
    """Record one /cplugapi/v1/* request. Called from the log handler.

    Public so a future direct caller (e.g. an in-process integration
    test that wants to bypass the logging layer) can drive the
    registry without going through Python logging.
    """
    _registry.record_request(method, path, status, dur_ms)


def observe_idempotency_replay() -> None:
    """Bump the idempotency replay counter."""
    _registry.idempotency_replays_total.inc()


def reset() -> None:
    """Clear every metric — test-only."""
    _registry.reset()


# ---------------------------------------------------------------------------
# Logging-handler integration
# ---------------------------------------------------------------------------


class _MetricsLogHandler(logging.Handler):
    """Translate ``cplugapi.access`` log records into metric observations.

    The :mod:`access_log` middleware emits one record per request with
    ``method``, ``path``, ``status``, ``dur_ms``, and ``replayed`` on
    ``record`` (via the ``extra`` argument to ``logger.info``). We
    don't touch the message body — using ``extra`` keeps us robust to
    formatter changes upstream (W14's JSON-mode toggle would otherwise
    break a parse-the-message approach).

    Errors in :meth:`emit` are swallowed by the standard
    ``logging.Handler.handleError`` path so a metrics bug can't break
    request handling. The handler is intentionally cheap: a few
    ``getattr`` reads and a single dict update per request.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            method = getattr(record, "method", None)
            path = getattr(record, "path", None)
            status = getattr(record, "status", None)
            dur_ms = getattr(record, "dur_ms", None)
            replayed = getattr(record, "replayed", False)
            if method and path and status is not None:
                _registry.record_request(
                    str(method), str(path), int(status), dur_ms
                )
            if replayed:
                _registry.idempotency_replays_total.inc()
        except Exception:  # pragma: no cover — defensive
            self.handleError(record)


_HANDLER_INSTALLED_FLAG = "_cplugapi_metrics_handler"
_install_lock = threading.Lock()


def install_handler() -> None:
    """Attach the metrics handler to ``cplugapi.access``. Idempotent.

    A module-level flag guards against duplicate installation under
    ``setup_cplugapi`` re-entry (test reuse, webui reload). The flag
    lives on the logger itself rather than a module-level global so
    a fresh import in a sub-interpreter still installs cleanly.
    """
    with _install_lock:
        logger = logging.getLogger("cplugapi.access")
        if getattr(logger, _HANDLER_INSTALLED_FLAG, False):
            return
        handler = _MetricsLogHandler()
        # INFO is the level access_log emits at; lower would be wasted.
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        setattr(logger, _HANDLER_INSTALLED_FLAG, True)


def uninstall_handler() -> None:
    """Detach the metrics handler — test-only.

    Iterates because ``install_handler`` is idempotent at the flag
    level but a misbehaving test setup could in theory have added
    multiple instances.
    """
    with _install_lock:
        logger = logging.getLogger("cplugapi.access")
        for h in list(logger.handlers):
            if isinstance(h, _MetricsLogHandler):
                logger.removeHandler(h)
        if hasattr(logger, _HANDLER_INSTALLED_FLAG):
            delattr(logger, _HANDLER_INSTALLED_FLAG)


# ---------------------------------------------------------------------------
# Active-task gauge sampling (lazy)
# ---------------------------------------------------------------------------


def _active_task_present() -> int:
    """Return 1 if ``modules.progress.current_task`` is non-null, else 0.

    Imported lazily so the metrics module can be loaded in test
    environments where ``modules.progress`` is the lightweight stub.
    Any error is treated as 0 (gauge fails closed — better to under-
    report than crash a scrape).
    """
    try:
        from modules import progress as progress_mod
    except Exception:
        return 0
    try:
        current = getattr(progress_mod, "current_task", None)
    except Exception:
        return 0
    return 1 if current else 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_counter(c: _Counter) -> list[str]:
    out: list[str] = []
    out.append(f"# HELP {c.name} {c.help}")
    out.append(f"# TYPE {c.name} counter")
    snapshot = c.snapshot()
    # Sort for deterministic output — Prometheus doesn't require it,
    # but tests + diffs are easier on a stable ordering.
    snapshot.sort(key=lambda kv: kv[0])
    for labels, count in snapshot:
        out.append(f"{c.name}{_format_labels(labels)} {count}")
    return out


def _render_scalar_counter(c: _ScalarCounter) -> list[str]:
    return [
        f"# HELP {c.name} {c.help}",
        f"# TYPE {c.name} counter",
        f"{c.name} {c.value()}",
    ]


def _render_histogram(h: _Histogram) -> list[str]:
    out: list[str] = []
    out.append(f"# HELP {h.name} {h.help}")
    out.append(f"# TYPE {h.name} histogram")
    snapshot = h.snapshot()
    snapshot.sort(key=lambda kv: kv[0])
    for labels, entry in snapshot:
        # Bucket lines: cumulative counts at each ``le=...``.
        for i, ub in enumerate(h.buckets):
            line_labels = labels + (("le", _format_float(ub)),)
            out.append(
                f"{h.name}_bucket{_format_labels(line_labels)} "
                f"{_format_float(entry[i])}"
            )
        # +Inf bucket — always equals the total observation count.
        inf_labels = labels + (("le", "+Inf"),)
        out.append(
            f"{h.name}_bucket{_format_labels(inf_labels)} "
            f"{_format_float(entry[len(h.buckets)])}"
        )
        # _count and _sum siblings.
        out.append(
            f"{h.name}_count{_format_labels(labels)} "
            f"{_format_float(entry[len(h.buckets)])}"
        )
        out.append(
            f"{h.name}_sum{_format_labels(labels)} "
            f"{_format_float(entry[len(h.buckets) + 1])}"
        )
    return out


def _render_active_task_gauge() -> list[str]:
    name = "cplugapi_active_task_id_present"
    help_text = (
        "1 when modules.progress.current_task is non-null, 0 otherwise."
    )
    return [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
        f"{name} {_active_task_present()}",
    ]


def _format_float(value: float) -> str:
    """Format a float for the Prometheus exposition format.

    Integral values render without a trailing ``.0`` (matches what
    ``prometheus_client`` produces and keeps test fixtures readable).
    """
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def render() -> str:
    """Produce the full Prometheus exposition body.

    Trailing newline is required by the spec (some scrapers tolerate
    its absence; we don't rely on that).
    """
    lines: list[str] = []
    lines.extend(_render_counter(_registry.requests_total))
    lines.append("")
    lines.extend(_render_histogram(_registry.request_duration))
    lines.append("")
    lines.extend(_render_scalar_counter(_registry.idempotency_replays_total))
    lines.append("")
    lines.extend(_render_active_task_gauge())
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Router wiring
# ---------------------------------------------------------------------------


def is_public() -> bool:
    """Whether ``CPLUG_METRICS_PUBLIC`` is set.

    Read each call (cheap env lookup) so an integration test can flip
    the var with ``monkeypatch.setenv`` without re-importing the
    module. The router caller should consult this once at mount time
    to decide which sub-router to attach the endpoint to.
    """
    raw = os.environ.get(_ENV_PUBLIC)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def attach(router: APIRouter) -> None:
    """Attach ``GET /metrics`` to ``router``.

    The caller (``router.py``) decides whether to attach to the public
    or the private sub-router based on :func:`is_public`. Wiring is
    deliberately kept out of this module so the auth posture decision
    stays visible in ``router.py:_do_mount``.
    """

    @router.get("/metrics")
    def metrics() -> Response:  # pragma: no cover — exercised via TestClient
        return Response(content=render(), media_type=CONTENT_TYPE)


def register_capabilities() -> None:
    """Advertise that this build exposes a Prometheus metrics endpoint.

    Always-on when the wiring code calls this — operators infer the
    auth posture from ``CPLUG_METRICS_PUBLIC`` or the OpenAPI spec, not
    from the capability string. Keeping the flag binary matches the
    rest of the registry.
    """
    capabilities.register("observability/metrics")


# ---------------------------------------------------------------------------
# Inspection helpers (test-only)
# ---------------------------------------------------------------------------


def _registry_for_tests() -> _Registry:
    """Hand a test the live registry. Not part of the public API."""
    return _registry


def _cardinality() -> dict[str, Any]:
    """Cardinality snapshot — diagnostic only.

    Surfaced for tests asserting the cap behaviour; a future
    ``/cplugapi/v1/_diag/metrics`` endpoint could expose this if
    debugging cardinality regressions in the field becomes a thing.
    """
    return {
        "requests_total": _registry.requests_total.cardinality(),
        "request_duration": _registry.request_duration.cardinality(),
        "cap": _CARDINALITY_CAP,
    }
