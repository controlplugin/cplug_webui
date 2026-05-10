# 2026-05-10 — Prometheus / OpenMetrics endpoint (W10)

**Kind**: new feature, observability surface.
**Files**: `modules/cplugapi/metrics.py` (new),
`tests/cplugapi/test_metrics.py` (new).
**Capability**: `observability/metrics`.
**Rollback**: drop the new module + test, drop the
`metrics.attach()` / `metrics.install_handler()` /
`metrics.register_capabilities()` calls from `router.py`.

## Symptom

Cloud operators and any future Grafana / Loki / k8s-monitoring
deployment have no machine-readable view of cplugapi liveness shape
— there is no way to scrape p50/p99 latency, per-route error rates,
or idempotency-replay pressure without parsing log lines after the
fact. The existing `gen_timing` instrument is a log stream, not a
metric, so any latency dashboard depends on the operator running
their own log pipeline. The world-class plan §3 W10 names this as
F11 ("no metrics endpoint") and gates production deployment behind
fixing it.

## Root cause

The cplugapi surface emits structured access-log lines on the
`cplugapi.access` logger (W9 access_log + W14 JSON-mode work
already shipped). All the data Prometheus needs — method, path,
status, duration — is on every record. What's missing is the
exposition surface and a counter/histogram registry to roll up the
records into series. The pre-W10 surface had access-log
*observation* but no *aggregation*.

## Alternatives considered

### Option A — depend on `prometheus_client`

Use the upstream Python client library. It handles every spec
detail (label escaping, exposition format, multiprocess merging,
HELP/TYPE block ordering) for free.

**Pre-spike result**: `prometheus_client` is not installed in the
project venv (`python -c "import prometheus_client"` errors). The
W10 plan body permits a vendored 50-LoC formatter when this is the
case. Adding it as a hard dep means another wheel in every
deployment for ~200 lines of behaviour we can write inline; the
fork is single-process, no multiprocess merging concern, and the
metric set is small enough that we can audit the formatter ourselves.

**Rejected** for "vendor a small formatter".

### Option B — depend on OpenTelemetry metrics SDK

Same idea as A, but via the OTel metrics surface so the same
instrument feeds Prometheus *and* OTLP-compatible backends. The W11
trace-context work will already pull in OpenTelemetry tracing.

**Rejected** because (a) the OTel metrics SDK is not currently
installed either, (b) OTel metrics are not byte-identical with the
text/0.0.4 exposition format Prometheus scrapes natively — a
Prometheus scraper would need an exporter sidecar — and (c) coupling
metrics to OTel before W11 lands creates a back-out hazard if W11
slips.

### Option C — instrument inside `access_log.py`

The access-log middleware already has every value we need; tacking
a metrics call onto its emit path is one line.

**Rejected** because the W10 task body explicitly forbids modifying
`access_log.py` ("integrate via Python logging handler"). The
rationale is the rebase-against-upstream invariant — the access-log
emit path is mostly upstream-stable but we want the metrics
integration to be self-contained in case upstream ever pulls our
access-log work in (or evolves their own).

### Option D — instrument as a FastAPI middleware

Stack a metrics middleware ahead of the access-log layer and
observe per-request from there.

**Rejected** because it duplicates the latency-measurement work
(two `perf_counter()` reads per request), and it would either
sit *inside* the access-log layer (under-counting overhead) or
*outside* (drifting from the access-log's wall clock — the very
number operators correlate when triaging). The handler approach
reuses access-log's authoritative `dur_ms` so the metric and the
log line agree by construction.

## Decision

**Vendored 50-ish-LoC text/0.0.4 formatter** in a new
`modules/cplugapi/metrics.py`. Counters, scalar counter, and
histogram primitives are hand-rolled — small enough to read in one
sitting, all thread-safe, all label-escaping-correct.

**Integration via `logging.Handler` on `cplugapi.access`.** A
`_MetricsLogHandler.emit` reads `record.method`, `record.path`,
`record.status`, `record.dur_ms`, and `record.replayed` off the
`extra` that access_log already populates. `access_log.py` is not
touched. `install_handler()` is idempotent (guards on a flag set
on the logger itself).

**Metric set (matches the world-class plan §3 W10):**

| Name | Kind | Labels | Source |
|---|---|---|---|
| `cplugapi_requests_total` | counter | method, path, status | log handler |
| `cplugapi_request_duration_seconds` | histogram | method, path | log handler |
| `cplugapi_idempotency_replays_total` | counter | — | log handler (via `replayed` flag) + direct API |
| `cplugapi_active_task_id_present` | gauge | — | sampled at scrape from `modules.progress.current_task` |

Histogram buckets: `[5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms,
1s, 2.5s, 5s, 10s, +Inf]`. Same set every Prometheus tutorial
ships — covers the sub-50ms region where health/queue/identify
polls live and the multi-second region where actual gens land.

**Path normalisation** maps templated routes to their template form:

- `/cplugapi/v1/session/cancel/task(txt2img-XYZ)` →
  `/cplugapi/v1/session/cancel/{id_task}`
- `/cplugapi/v1/forge/preset/default` →
  `/cplugapi/v1/forge/preset/{name}`

The mapping is an explicit prefix table (`_TEMPLATED_PREFIXES`) —
auditable at a glance, survives upstream rebases, surfaces the
coverage gap the moment a new templated route is added.

**Cardinality cap**: 100 distinct `(method, path, status)` series.
Beyond that, novel paths bin under `path="<other>"`. Defence-in-
depth against an attacker who finds a cplugapi route that doesn't
canonicalise its path label.

**Auth posture**: endpoint mounts on the **private** router by
default (inherits `--api-auth`). `CPLUG_METRICS_PUBLIC=1` flips
`metrics.is_public()` to `True` so the wiring code in `router.py`
can move it to the public router (cloud sidecar pattern).

## Blast radius

- **No invariant violation.** `/sdapi/v1/*` is untouched. New
  routes are namespaced under `/cplugapi/v1/metrics`. Capability
  string is slash-form (`observability/metrics`).
- **No new hard dependency.** Module imports only stdlib + FastAPI
  primitives the fork already pulls.
- **Access-log middleware unchanged.** Integration is via Python
  logging — non-invasive, easy to reason about, easy to remove.
- **No upstream contact**. Module lives entirely under
  `modules/cplugapi/`. Nothing in `backend/` or `modules/`
  outside the package is touched.
- **Wiring is the user's responsibility.** This change exposes
  `attach(router)`, `install_handler()`, `register_capabilities()`,
  and `is_public()` — `router.py` decides when/where to call them.

## Failure modes

1. **Cardinality blow-out** — an attacker hammering unique paths
   would normally explode the registry. Mitigated by the
   normalisation table (collapses templated paths before the
   counter sees them) and by the 100-series cap (overflow → bucket).
2. **Unjsonable / non-string labels** — `record.method` etc. are
   coerced via `str()` before they hit the formatter. The label
   escaper handles backslash, double-quote, and newline (the
   three characters Prometheus's text format requires escaped); a
   round-trip test asserts a label value containing all three
   parses cleanly through the test's exposition parser.
3. **Handler exceptions** — `_MetricsLogHandler.emit` swallows
   exceptions through the standard `logging.Handler.handleError`
   path. A bug in metrics cannot break request handling.
4. **Logger contention** — adding a handler to `cplugapi.access`
   means every access-log emission walks one extra handler
   `emit()`. The handler does ~5 dict reads + 2 `_Counter.inc`s
   per request; benchmarking is unnecessary at expected request
   rates (10–100 req/s desktop loopback, < 1 req/s cloud-deploy
   probe traffic).
5. **Active-task gauge fails closed** — if `modules.progress`
   import or attribute access raises, the gauge reports 0
   rather than crashing the scrape. Documented in the source.
6. **`reset()` isn't fully thread-safe vs. `record_request`** —
   intentional. `reset()` is test-only and tests don't run
   handler-fed observations in parallel with `reset()`. Production
   code path never resets.

## Test surface

`tests/cplugapi/test_metrics.py` (31 tests):

- **Endpoint contract**: `Content-Type` is `text/plain; version=0.0.4;
  charset=utf-8`; HELP + TYPE comments are present for every metric
  family (counter, histogram, gauge).
- **Counter increment**: 3 hits to `/health` → counter sample with
  count 3; 4xx hits land on a separate series from 2xx.
- **Histogram cumulativity**: bucket counts are monotonically
  non-decreasing along increasing `le`; `+Inf` bucket equals the
  total observation count; `_count` mirrors `+Inf`.
- **Idempotency replay**: counter increments via the
  `Idempotency-Replayed: true` flow on the access-log path AND
  via the direct `observe_idempotency_replay()` helper.
- **Path normaliser**: `session/cancel/task(...)` and
  `forge/preset/{name}` both collapse correctly; static routes
  pass through untouched; root-prefix-only paths are not
  accidentally collapsed.
- **Cardinality cap**: registers 100 synthetic paths, confirms
  series 101 and 102 collapse into `<other>`.
- **Label escaping**: backslash, double-quote, and newline all
  round-trip through the formatter and a tiny in-test parser.
  A separate test feeds an ugly path through `observe_request`
  and confirms the rendered body is parseable.
- **Active-task gauge**: reads `modules.progress.current_task`
  via the test's `progress_stub`; toggles 0 ↔ 1.
- **Capability**: `observability/metrics` appears in
  `enabled_capabilities()` after `register_capabilities()`.
- **`is_public()` env-var matrix**: `1`/`true`/`yes`/`on` (any
  case) → True; `0`/`false`/`no`/`off`/empty → False; unset →
  False.
- **Handler idempotence**: `install_handler()` called three
  times produces exactly one handler instance on the logger.
- **Rendering determinism**: same registry state ⇒ identical
  rendered body across calls.

Full cplugapi suite: 530 pass, 4 skip (the skips are pre-existing,
torch-dependent, unrelated). No regressions introduced.
