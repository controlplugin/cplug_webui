# 2026-05-10 — W3C Trace Context propagation (W11)

**Kind**: new pure-ASGI middleware that reads, validates, and echoes
the W3C `traceparent` header across `/cplugapi/v1/*`. Header echo
only — no span emission, no SDK dependency.
**Files**: `modules/cplugapi/tracing.py` (new),
`tests/cplugapi/test_tracing.py` (new).
**Capability**: `observability/trace-context-w3c` (path-scoped to
`/cplugapi/v1/*`; advertised on `/identify` and `/health` once
`router.py` calls `tracing.register_capabilities()`).
**Rollback**: drop the `tracing.install(app)` + capability call in
`router._install_middlewares` / `_register_capabilities`, delete the
module + tests. Distributed tracers fall back to ignoring the header
on the cplugapi surface (status quo before W11).

## Symptom

The fork carried no propagation of the W3C Trace Context spec
(https://www.w3.org/TR/trace-context/). Distributed-tracing systems
that span the desktop client, the cplugapi backend, and any future
worker fanout could not stitch a single trace across the boundary —
each service started its own root trace because nothing on the wire
identified the parent. For an operator running cplugapi behind a
trace-aware reverse proxy or sidecar (Datadog/Honeycomb/Tempo), the
backend was a black hole in the trace tree even though every other
hop was instrumented.

This shows up specifically as a gap rather than a bug: the upstream
A1111-derived stack predates broad otel adoption, and the cplugapi
fork never claimed trace support. World-class hardening §3 W11 calls
this out as a baseline observability requirement before any otel-SDK
integration can be considered.

## Root cause

Two separate gaps:

1. **No reader.** Without code that parses an inbound `traceparent`,
   the surface ignores the upstream context entirely. A trace-aware
   proxy that sets `traceparent` on every forwarded request gets the
   header dropped silently — there is no point at which downstream
   logic could pick it up.
2. **No emitter.** Even a trace started server-side has no way to
   surface its id to a client. A traceparent on the response is the
   contract the W3C spec leans on: HTTP middleware that wants to
   correlate request → response (a sidecar wrapping the backend, an
   OpenTelemetry collector, log-line stitching) needs the canonical
   id on the response headers.

Both gaps are addressed by header-echo middleware. SDK integration
(span creation, sampling, exporter wiring) is a separate problem
gated on `opentelemetry-sdk` actually being installed — the
pre-spike confirmed it is not, so attempting auto-detect would be a
silent no-op rather than a feature.

## Alternatives considered

### Option A — auto-detect `opentelemetry`, install otel auto-instrumentation when present

The "do nothing if SDK is missing, do everything if it's installed"
posture. Ship today, become useful tomorrow when someone
`uv add opentelemetry-sdk`'s into their venv.

**Rejected.** The capability advertisement on `/identify` is a
contract: clients enabling trace-aware behaviour off the
`observability/trace-context-w3c` string need to know what behaviour
they're getting. Auto-detect means the same capability string covers
two distinct posture levels (header-echo vs full SDK integration),
which is exactly the dot-vs-slash ambiguity the capability registry
was designed to avoid. When otel-sdk lands, a separate capability
`observability/trace-context-w3c-spans` (or similar) advertises the
upgrade. Header-echo is meaningful on its own — it's the foundation
the W3C spec was designed around.

### Option B — Zipkin / B3 propagation instead of W3C

`X-B3-TraceId` / `X-B3-SpanId` / `X-B3-Sampled`. Older but still in
use. Some Java-shop tooling defaults to it.

**Rejected.** W3C is the IETF / W3C standard. Modern otel SDKs read
both formats, but the canonical wire format is W3C. Implementing two
formats for header-echo doubles the surface for no clear benefit;
clients that need B3 can convert at their proxy layer.

### Option C — extend `request_id.py` with a second header field

The plan body suggested "extend request_id.py OR new sibling
tracing.py". Same `BaseHTTPMiddleware` shell, second header name,
shared validation.

**Rejected.** `request_id.py` uses `BaseHTTPMiddleware` (response
buffering, doesn't coexist cleanly with WS upgrades or streaming
responses). Tracing must be pure-ASGI for the same WebSocket-coexist
reason `ws_auth.py` and `auto_preempt.py` are pure-ASGI. Shoehorning
tracing into the same middleware base would either regress those
properties or require a one-off pure-ASGI carve-out inside
`request_id.py` — at which point it's two modules sharing a file.
Sibling module is cleaner and matches the cplugapi convention of
"one concern per middleware module".

### Option D — only echo on response, never read inbound

Cheaper: every cplugapi response gets a fresh server-side
traceparent, no parsing or validation. Still useful for correlating
within the backend.

**Rejected.** The whole point of W3C Trace Context is `parent-id`
chaining across hops. Discarding inbound context means the cplugapi
backend always starts a new root trace — operators running cplugapi
behind a trace-aware ingress see two disconnected traces (ingress's
root → ingress's child, backend's new root → backend's child).
Header-echo is the minimum-viable cooperation; reading the inbound
value is what makes the propagation actually work.

## Decision

`modules/cplugapi/tracing.py` exports a pure-ASGI middleware
(`CplugapiTracingMiddleware`) installed into `app.user_middleware`
with the same idempotent + thread-safe guard pattern the other
cplugapi middlewares use.

On every `/cplugapi/v1/*` HTTP request:

1. Look for `traceparent` in the inbound headers (lower-case bytes
   key — ASGI normalises to lower-case).
2. Run it through `_validate_traceparent`, which gates on
   *all* of: shape regex (version-traceid-parentid-flags, hex,
   correct lengths), non-zero trace-id, non-zero parent-id. The
   regex is anchored and case-sensitive lower-case (W3C §3.2.2.4 —
   the spec defines hex chars as 0-9 a-f).
3. If validation fails OR the header is absent, generate a fresh
   traceparent (`secrets.token_hex` for the random fields,
   `version=00`, `flags=00`).
4. Stash the canonical full string on `scope["state"]["traceparent"]`
   and the bare 32-char trace-id on `scope["state"]["trace_id"]`. The
   existing `request.state.x` access pattern reads through the same
   dict so handlers can use `tracing.get_traceparent(request)` /
   `tracing.get_trace_id(request)`.
5. Wrap `send` so the canonical traceparent is appended to the
   `http.response.start` headers. Any inbound traceparent on the
   response (shouldn't happen — this is the canonical source) is
   stripped first so the wire carries exactly one value.

WebSocket scopes pass through untouched — the W3C spec is HTTP-shaped
and a future T31 endpoint should attach trace context to its own
upgrade flow if it needs it. Lifespan and other non-HTTP scopes
short-circuit at the same `type != "http"` check.

Out-of-prefix paths (`/sdapi/v1/*`, Gradio routes, etc.) short-circuit
at the path check so invariant 1 (byte-identity for `/sdapi/v1/*`)
is preserved — verified by
`test_does_not_apply_outside_prefix_even_with_inbound`.

### W3C all-zero validation

Per spec §3.2.2.5 and §3.3.2.5, all-zero `trace-id` and `parent-id`
are explicitly invalid even though they pass the shape regex. Vendors
SHOULD restart the trace in that case. The middleware does exactly
that — `_validate_traceparent` returns None for both forms and the
caller mints a fresh traceparent. Two unit tests
(`test_validate_rejects_all_zero_trace_id`,
`test_validate_rejects_all_zero_parent_id`) and one integration test
(`test_inbound_all_zero_trace_id_replaced`) cover it.

### No `opentelemetry` import

The plan ruled out auto-detect (Option A above) and the module is
careful to never import `opentelemetry` — even guarded by `try /
except ImportError`. A future capability
`observability/trace-context-w3c-spans` can land alongside an
explicit `opentelemetry-sdk` dependency.

## Blast radius

- Cplugapi responses: gain a `traceparent` header. Clients that
  ignore the header are unaffected (HTTP allows arbitrary unknown
  response headers). Clients that consume it now get distributed-trace
  correlation for free.
- `/sdapi/v1/*` responses: zero change. Invariant 1 preserved.
- `request.state` namespace: gains two fields (`traceparent`,
  `trace_id`). Existing `request.state.request_id` is untouched —
  the modules are siblings.
- Observability dependencies: none added. `opentelemetry-sdk` is NOT
  installed and is NOT imported by this module.
- Wire format: 70-byte fixed-length `traceparent` header on every
  cplugapi response. Negligible bandwidth impact.

## Failure modes

1. **Inbound traceparent uses upper-case A-F hex** — the W3C spec
   requires lower-case; the validator rejects upper-case and mints
   a fresh traceparent. Some older instrumentation libraries emit
   upper-case; those clients would silently start a new trace
   server-side. Acceptable — the W3C-conforming fix is on their end,
   and the request still completes. A future relax of the regex to
   accept upper-case (and lower-case it on echo) is a 1-line change
   if it becomes operationally annoying.
2. **`scope["state"]` already populated by an upstream middleware**
   — we update existing keys rather than replacing the dict. Other
   keys are untouched. If a sibling middleware also sets `traceparent`
   (it shouldn't — this module is the canonical source), it would
   clobber ours since we run early; the response-side wrap uses the
   value captured at request time so the wire is consistent
   regardless.
3. **Inner app emits its own `traceparent` header on the response**
   — the wrapped `send` strips it before appending the canonical
   value. The wire carries exactly one traceparent.
4. **W3C spec adds a `version=01` with extended fields** — the
   validator only accepts `version=00`. A `01` traceparent would be
   replaced with a fresh `00` one server-side, dropping any extended
   fields. Acceptable until `01` actually ships; the 2-line update
   is to add the new version's regex branch.
5. **Bytes header decode error** — `value.decode("ascii")` is
   wrapped in try/except UnicodeDecodeError. Garbage-bytes headers
   become "no inbound" and a fresh traceparent is minted.

## Test surface

`tests/cplugapi/test_tracing.py` — 17 cases:

- Generator: matches W3C regex, non-zero ids, version=`00`, flags=`00`,
  unique across 32 calls.
- Validator: accepts well-formed; rejects empty, garbage, missing
  segments, extra segments, wrong-length segments, non-hex chars,
  spaces, upper-case, all-zero trace-id, all-zero parent-id.
- Middleware (TestClient): generates when absent; echoes valid
  inbound verbatim; replaces malformed inbound; replaces all-zero
  trace-id; response always carries the header; handler reads
  `request.state.traceparent` + `request.state.trace_id` via the
  helpers.
- Path scoping: `/sdapi/v1/_test/foo` does NOT get a traceparent
  response header even when the inbound request supplied one.
  (Forward-checked attaching a stub `/sdapi/v1/_test/foo` route on
  the test app — invariant 1.)
- Direct ASGI: WebSocket scope passes through with no synthesised
  messages; lifespan scope passes through; `install` is idempotent.
- Capability: `register_capabilities()` adds
  `observability/trace-context-w3c`.

Full cplugapi suite: 511 passing, 4 skipped (no regressions). The
new module + tests do not alter any existing test outcome.
