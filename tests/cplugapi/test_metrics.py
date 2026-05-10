"""Tests for ``modules.cplugapi.metrics``.

Coverage is structured around the W10 acceptance criteria:

1. Observed requests (via the access-log handler integration) increment
   the request counter and feed the duration histogram.
2. Histogram bucket counts are cumulative.
3. Idempotency replay counter increments via the ``replayed`` flag on
   the access-log record AND via the direct ``observe_idempotency_replay``
   helper.
4. Path normaliser collapses templated routes into their template form.
5. Cardinality cap binds the spew of unique paths to ``<other>``.
6. Label escaping survives backslash, double-quote, and newline.
7. The endpoint returns the spec-required Content-Type.
8. ``observability/metrics`` is registered.
9. ``CPLUG_METRICS_PUBLIC`` env var flips the ``is_public`` flag.

The render output is parsed with a minimal exposition-format
parser (~30 LoC, in ``_parse_prom``) so assertions read against
labelled values rather than substring-matching the text body.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, capabilities, metrics, setup_cplugapi


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_metrics_state():
    """Each test starts with an empty registry + a freshly attached
    handler. We tear the handler down in teardown so an earlier test's
    handler doesn't double-observe a later test's requests.
    """
    metrics.uninstall_handler()
    metrics.reset()
    metrics.install_handler()
    yield
    metrics.uninstall_handler()
    metrics.reset()


@pytest.fixture
def access_logger_propagates():
    """Access-log records ride the ``cplugapi.access`` logger which has
    ``propagate=False`` set by Forge's ``setup_logger``. We don't need
    propagation for metrics (we attach our own handler), but flipping it
    on while the test runs lets pytest's caplog also see the records
    for diagnostic assertions if a test wants them.
    """
    logger = logging.getLogger("cplugapi.access")
    original = logger.propagate
    logger.propagate = True
    yield logger
    logger.propagate = original


def _make_client() -> TestClient:
    app = FastAPI()
    setup_cplugapi(app)
    # Wire the metrics endpoint manually for tests — production wiring
    # lives in router.py and is the user's responsibility (this test
    # module does not touch router.py).
    extra = APIRouter()
    metrics.attach(extra)
    app.include_router(extra, prefix=PREFIX)
    return TestClient(app)


def _parse_prom(body: str) -> dict[str, Any]:
    """Tiny Prometheus exposition parser.

    Returns a dict keyed by metric *family name* (e.g.
    ``cplugapi_requests_total``). Values are lists of
    ``(label_dict, float_value)`` tuples for samples whose name
    matches the family base; histogram suffixes (``_bucket``,
    ``_sum``, ``_count``) are bucketed under separate keys
    (``..._bucket``, ``..._sum``, ``..._count``) so tests can
    target them directly.

    Skipped: ``# HELP`` / ``# TYPE`` lines (we assert their presence
    via raw substring matches separately) and blank lines.
    """
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    name_re = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)")
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        m = name_re.match(line)
        if not m:
            raise AssertionError(f"unparseable line: {line!r}")
        name = m.group(1)
        rest = line[m.end():]
        labels: dict[str, str] = {}
        if rest.startswith("{"):
            # Walk the labelset until a balancing ``}`` outside any
            # quoted region. Naive ``[^}]*`` regex breaks because label
            # values may contain ``{`` / ``}`` (we render templated
            # paths as ``{id_task}``).
            close = _find_unquoted_close_brace(rest, start=1)
            if close < 0:
                raise AssertionError(f"unterminated labelset: {line!r}")
            labels = _parse_labels(rest[1:close])
            rest = rest[close + 1:]
        rest = rest.strip()
        if not rest:
            raise AssertionError(f"missing value: {line!r}")
        try:
            value = float(rest.split()[0])
        except ValueError:
            raise AssertionError(f"unparseable value: {line!r}")
        out.setdefault(name, []).append((labels, value))
    return out


def _find_unquoted_close_brace(s: str, start: int) -> int:
    """Return the index of the first ``}`` outside a double-quoted region.

    Treats ``\\"`` as an escaped quote (does not toggle quote state),
    matching the Prometheus exposition format's escape rules.
    """
    in_quote = False
    i = start
    while i < len(s):
        ch = s[i]
        if in_quote:
            if ch == "\\" and i + 1 < len(s):
                i += 2
                continue
            if ch == '"':
                in_quote = False
        else:
            if ch == '"':
                in_quote = True
            elif ch == "}":
                return i
        i += 1
    return -1


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse a label set, honouring escaped quotes / backslashes.

    Implements the inverse of the metrics formatter's escaper.
    """
    labels: dict[str, str] = {}
    pos = 0
    while pos < len(raw):
        # Name = identifier up to ``=``.
        eq = raw.index("=", pos)
        name = raw[pos:eq].strip()
        # Value is between an opening ``"`` and an unescaped ``"``.
        if raw[eq + 1] != '"':
            raise AssertionError(f"missing opening quote at {raw!r}")
        i = eq + 2
        chunks: list[str] = []
        while i < len(raw):
            ch = raw[i]
            if ch == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                chunks.append(
                    {"\\": "\\", '"': '"', "n": "\n"}.get(nxt, nxt)
                )
                i += 2
                continue
            if ch == '"':
                break
            chunks.append(ch)
            i += 1
        labels[name] = "".join(chunks)
        # Skip closing quote, optional comma.
        pos = i + 1
        if pos < len(raw) and raw[pos] == ",":
            pos += 1
    return labels


def _hit_health(client: TestClient) -> None:
    """One observable cplugapi request — the access-log middleware will
    log it, the metrics handler will count it."""
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_endpoint_content_type_is_prometheus(progress_stub):
    client = _make_client()
    r = client.get(f"{PREFIX}/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"] == metrics.CONTENT_TYPE


def test_render_includes_help_and_type_lines(progress_stub):
    client = _make_client()
    body = client.get(f"{PREFIX}/metrics").text
    # Each metric must announce HELP + TYPE per spec.
    assert "# HELP cplugapi_requests_total " in body
    assert "# TYPE cplugapi_requests_total counter" in body
    assert "# HELP cplugapi_request_duration_seconds " in body
    assert "# TYPE cplugapi_request_duration_seconds histogram" in body
    assert "# HELP cplugapi_idempotency_replays_total " in body
    assert "# TYPE cplugapi_idempotency_replays_total counter" in body
    assert "# HELP cplugapi_active_task_id_present " in body
    assert "# TYPE cplugapi_active_task_id_present gauge" in body


def test_request_counter_increments_per_request(
    progress_stub, access_logger_propagates
):
    client = _make_client()
    _hit_health(client)
    _hit_health(client)
    _hit_health(client)
    body = client.get(f"{PREFIX}/metrics").text
    parsed = _parse_prom(body)
    health_samples = [
        (lbls, v)
        for lbls, v in parsed["cplugapi_requests_total"]
        if lbls.get("path") == f"{PREFIX}/health"
    ]
    assert len(health_samples) == 1
    labels, count = health_samples[0]
    assert labels["method"] == "GET"
    assert labels["status"] == "200"
    # We hit /health 3 times before scraping; the scrape itself is on
    # /metrics so it doesn't pollute the /health series.
    assert count == 3


def test_histogram_buckets_are_cumulative(progress_stub, access_logger_propagates):
    """For every bucket ``le[i] <= le[i+1]``, the cumulative count must
    not decrease — that's the contract Prometheus assumes when
    computing histogram_quantile."""
    client = _make_client()
    for _ in range(5):
        _hit_health(client)
    body = client.get(f"{PREFIX}/metrics").text
    parsed = _parse_prom(body)
    health_buckets = [
        (lbls, v)
        for lbls, v in parsed["cplugapi_request_duration_seconds_bucket"]
        if lbls.get("path") == f"{PREFIX}/health"
    ]
    # Sort by ``le`` ascending; ``+Inf`` sorts last.
    def _le_key(item: tuple[dict[str, str], float]) -> float:
        v = item[0]["le"]
        return float("inf") if v == "+Inf" else float(v)

    health_buckets.sort(key=_le_key)
    counts = [v for _, v in health_buckets]
    assert counts, "expected at least one bucket sample"
    for prev, nxt in zip(counts, counts[1:]):
        assert nxt >= prev, f"non-cumulative buckets: {counts}"
    # The +Inf bucket equals the total observation count (5 hits).
    assert counts[-1] == 5
    # _count sample exists and matches +Inf bucket.
    count_samples = [
        v
        for lbls, v in parsed["cplugapi_request_duration_seconds_count"]
        if lbls.get("path") == f"{PREFIX}/health"
    ]
    assert count_samples == [5]


def test_idempotency_replay_counter_via_helper(progress_stub):
    metrics.observe_idempotency_replay()
    metrics.observe_idempotency_replay()
    body = metrics.render()
    parsed = _parse_prom(body)
    samples = parsed["cplugapi_idempotency_replays_total"]
    assert len(samples) == 1 and samples[0][1] == 2


def test_idempotency_replay_counter_via_log_handler(
    progress_stub, access_logger_propagates
):
    """The /forge/preset endpoint is idempotent-cacheable. Two calls
    with the same key should produce one replay observation."""
    from modules.cplugapi import idempotency

    idempotency.reset_cache()
    client = _make_client()
    headers = {"Idempotency-Key": "metric-replay-key-1"}
    r1 = client.post(f"{PREFIX}/forge/preset/default", headers=headers)
    r2 = client.post(f"{PREFIX}/forge/preset/default", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    body = client.get(f"{PREFIX}/metrics").text
    parsed = _parse_prom(body)
    samples = parsed["cplugapi_idempotency_replays_total"]
    assert len(samples) == 1 and samples[0][1] == 1


def test_path_normaliser_session_cancel():
    assert (
        metrics._normalise_path(
            "/cplugapi/v1/session/cancel/task(txt2img-ABC123)"
        )
        == "/cplugapi/v1/session/cancel/{id_task}"
    )


def test_path_normaliser_forge_preset():
    assert (
        metrics._normalise_path("/cplugapi/v1/forge/preset/default")
        == "/cplugapi/v1/forge/preset/{name}"
    )
    assert (
        metrics._normalise_path("/cplugapi/v1/forge/preset/sd_xl_turbo")
        == "/cplugapi/v1/forge/preset/{name}"
    )


def test_path_normaliser_passthrough_for_static_routes():
    """Routes without templated trailing segments must not be rewritten."""
    for path in (
        "/cplugapi/v1/health",
        "/cplugapi/v1/identify",
        "/cplugapi/v1/version",
        "/cplugapi/v1/queue",
    ):
        assert metrics._normalise_path(path) == path


def test_path_normaliser_session_cancel_root_is_passthrough():
    """``/cplugapi/v1/session/cancel/`` (trailing slash, empty id_task)
    is not a real route — confirm we don't accidentally match it."""
    # Only paths longer than the prefix collapse; the bare prefix passes
    # through (a 404 would be served against the route, but the metrics
    # path label stays accurate).
    assert (
        metrics._normalise_path("/cplugapi/v1/session/cancel/")
        == "/cplugapi/v1/session/cancel/"
    )


def test_observe_request_normalises_path(progress_stub):
    metrics.observe_request(
        "POST",
        "/cplugapi/v1/session/cancel/task(txt2img-XYZ)",
        200,
        12.5,
    )
    body = metrics.render()
    parsed = _parse_prom(body)
    paths = {
        lbls.get("path") for lbls, _ in parsed["cplugapi_requests_total"]
    }
    assert "/cplugapi/v1/session/cancel/{id_task}" in paths
    # Concrete task id must NOT leak into the labelset.
    assert all("txt2img-XYZ" not in p for p in paths if p)


def test_cardinality_cap_buckets_overflow(progress_stub):
    """Once we've registered :data:`metrics._CARDINALITY_CAP` distinct
    series, further novel paths must collapse into ``<other>``."""
    cap = metrics._CARDINALITY_CAP
    # Use truly-novel synthetic paths so the normaliser doesn't
    # collapse them. They are not real routes; ``observe_request`` is
    # the in-process API and accepts any string.
    for i in range(cap):
        metrics.observe_request("GET", f"/cplugapi/v1/_synthetic/path-{i}", 200, 1.0)
    # At cap. Next novel path must spill.
    metrics.observe_request("GET", "/cplugapi/v1/_synthetic/overflow-A", 200, 1.0)
    metrics.observe_request("GET", "/cplugapi/v1/_synthetic/overflow-B", 200, 1.0)
    body = metrics.render()
    parsed = _parse_prom(body)
    paths = [lbls.get("path") for lbls, _ in parsed["cplugapi_requests_total"]]
    # The original ``cap`` series are present.
    assert sum(1 for p in paths if p and p.startswith("/cplugapi/v1/_synthetic/path-")) == cap
    # Overflow paths collapse into the sentinel bucket.
    assert "<other>" in paths


def test_label_escaping_handles_backslash_quote_newline():
    """The escaper must round-trip ugly label values through the
    formatter cleanly — Prometheus parsers will reject malformed
    output."""
    raw = 'a\\b"c\nd'
    escaped = metrics._escape_label_value(raw)
    # Each forbidden byte is escaped exactly once.
    assert "\\\\" in escaped
    assert '\\"' in escaped
    assert "\\n" in escaped
    # Round-trip via our parser confirms the text is decodable.
    rendered = (
        f'cplugapi_test{{path="{escaped}"}} 1\n'
    )
    parsed = _parse_prom(rendered)
    samples = parsed["cplugapi_test"]
    assert samples[0][0]["path"] == raw


def test_observe_request_with_escapable_path():
    """A malicious caller could in principle stuff a quote / backslash
    into the path. The label MUST come out clean and parseable."""
    metrics.observe_request("GET", '/cplugapi/v1/he"llo\\world', 200, 1.0)
    body = metrics.render()
    # Must be a parseable body — _parse_prom raises if not.
    parsed = _parse_prom(body)
    paths = [lbls.get("path") for lbls, _ in parsed["cplugapi_requests_total"]]
    assert '/cplugapi/v1/he"llo\\world' in paths


def test_active_task_gauge_reflects_progress_state(progress_stub):
    progress_stub.current_task = None
    body = metrics.render()
    parsed = _parse_prom(body)
    gauge = parsed["cplugapi_active_task_id_present"]
    assert gauge[0][1] == 0

    progress_stub.current_task = "txt2img-ABCDEF"
    body = metrics.render()
    parsed = _parse_prom(body)
    gauge = parsed["cplugapi_active_task_id_present"]
    assert gauge[0][1] == 1


def test_register_capabilities_advertises_observability_metrics(clean_capabilities):
    metrics.register_capabilities()
    enabled = capabilities.enabled_capabilities()
    assert "observability/metrics" in enabled


def test_is_public_default_is_false(monkeypatch):
    monkeypatch.delenv("CPLUG_METRICS_PUBLIC", raising=False)
    assert metrics.is_public() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "On"])
def test_is_public_truthy_env_values(monkeypatch, value):
    monkeypatch.setenv("CPLUG_METRICS_PUBLIC", value)
    assert metrics.is_public() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_is_public_falsy_env_values(monkeypatch, value):
    monkeypatch.setenv("CPLUG_METRICS_PUBLIC", value)
    assert metrics.is_public() is False


def test_render_is_deterministic_across_calls(progress_stub, access_logger_propagates):
    """Same registry state -> same rendered body. Important so a
    Prometheus scrape comparing two snapshots can compute a clean
    diff; non-deterministic ordering would inflate the apparent
    series churn in delta-encoded backends."""
    client = _make_client()
    _hit_health(client)
    _hit_health(client)
    a = metrics.render()
    b = metrics.render()
    assert a == b


def test_handler_install_is_idempotent():
    """Re-installing the handler must not stack duplicates — otherwise
    each request would be observed N+1 times."""
    metrics.install_handler()
    metrics.install_handler()
    metrics.install_handler()
    logger = logging.getLogger("cplugapi.access")
    handlers = [
        h for h in logger.handlers if isinstance(h, metrics._MetricsLogHandler)
    ]
    assert len(handlers) == 1


def test_request_counter_distinguishes_status(progress_stub, access_logger_propagates):
    """4xx and 2xx must render as separate series."""
    client = _make_client()
    # 200 — known route.
    client.get(f"{PREFIX}/health")
    # 404 — unknown preset name.
    client.post(f"{PREFIX}/forge/preset/__nope__")
    body = client.get(f"{PREFIX}/metrics").text
    parsed = _parse_prom(body)
    statuses = {
        (lbls.get("path"), lbls.get("status"))
        for lbls, _ in parsed["cplugapi_requests_total"]
    }
    assert (f"{PREFIX}/health", "200") in statuses
    assert ("/cplugapi/v1/forge/preset/{name}", "404") in statuses
