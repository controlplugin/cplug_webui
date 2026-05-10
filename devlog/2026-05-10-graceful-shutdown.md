# 2026-05-10 — Graceful shutdown via lifespan + SIGTERM bridge (W12)

**Kind**: new module + signal binding + optional reject-during-drain middleware.
**Files**: `modules/cplugapi/shutdown.py` (new),
`tests/cplugapi/test_shutdown.py` (new). Integrates with W1's
`livez_readyz.set_draining` (already in place).
**Capability**: `ops/graceful-shutdown`.
**Rollback**: revert `shutdown.install(app)` call in
`router._install_middlewares`. Module + tests can stay or go.

## Symptom

When the cplugapi process receives SIGTERM during a rolling restart
or pod replacement:

- In-flight gens get aborted at the kernel level (`SIGKILL` after
  the orchestrator's grace period). The artist sees a half-rendered
  preview disappear.
- The replacement replica accepts new gen requests *during* the old
  pod's drain window, double-billing GPU compute.
- The k8s readiness probe doesn't observe the impending shutdown
  until the pod is already gone, so the orchestrator routes new
  requests to a draining replica.

## Root cause

No shutdown handling. The fork inherits FastAPI's default behaviour
(no shutdown event), and `--api-server-stop` is upstream's mechanism
that just terminates the process.

## Decision

Three-phase graceful shutdown sequence triggered on SIGTERM (or by
explicit `await graceful_shutdown()` invocation, used by tests):

1. **Drain.** `livez_readyz.set_draining(True)` flips the drain
   flag — observable on `/readyz` (W1 already added this to the
   sanitised public body, so unauth k8s probes see
   `checks.draining=true` and pull the pod from rotation on their
   next poll, typically within 5–10 seconds).
2. **Wait for in-flight work.** Poll `progress.current_task` and
   `progress.pending_tasks` every `CPLUG_SHUTDOWN_POLL_INTERVAL_S`
   (default 0.5s) for up to `CPLUG_SHUTDOWN_GRACE_S` (default 30s).
   When both are empty, exit early.
3. **Interrupt remaining.** After grace expires, call
   `shared.state.interrupt()` to abort whatever's still running.
   Returns a report dict for log/metric integration.

Optional `RejectDuringDrainMiddleware`: when
`CPLUG_SHUTDOWN_REJECT_NEW=1` (or cloud profile default), POSTs to
`/cplugapi/v1/*` and `/sdapi/v1/{txt2img,img2img}` (the gen entry
points) return 503 with `Retry-After: 5` while the drain flag is
set. Reads pass through so capability/health probes continue
serving. Other `/sdapi/v1/*` paths (options, models, samplers,
progress, etc.) also pass through — they're metadata reads that
should stay reachable during drain.

### Lifespan vs. signal handler vs. on_event

The plan-eval round flagged that `@app.on_event("shutdown")` is
deprecated in FastAPI ≥0.93 in favour of the Starlette lifespan
context manager. But cplugapi mounts *post-launch* — by the time
`setup_cplugapi` runs, the FastAPI app has already been constructed
without `lifespan=`, and gradio's startup wiring is in place.
Hijacking `app.router.lifespan_context` post-construction is brittle.

Decision: use Python's `signal` module to bind SIGTERM at install
time. The signal handler bridges to the async sequence via
`loop.call_soon_threadsafe(asyncio.create_task, graceful_shutdown())`.
Signals fire on the main thread; the async sequence runs on the
event loop.

Caveats documented in-code: SIGTERM binding only works on the main
thread. The bind is wrapped in `try/except (ValueError, OSError,
AttributeError)` so non-main-thread test fixtures and Windows
environments don't crash on install. Tests invoke
`graceful_shutdown()` directly to exercise the state machine
without needing real signal delivery.

### Reject-during-drain default

- Desktop profile: REJECT_NEW default = False. Single-replica posture;
  the operator wants the in-flight gen to complete on shutdown, and
  there's no other replica to route to. Accepting new gens during
  drain "lies to the client" only insofar as those new gens get
  interrupted at grace expiry — but the grace window is generous
  (30s default) so most gens finish.
- Cloud profile: REJECT_NEW default = True. Multi-replica orchestration
  (within the §1 single-replica scope, this means rolling restarts
  with N=1 replicas — the orchestrator brings up the new replica
  before terminating the old) wants the old pod to refuse new work
  so the orchestrator routes to the new replica.

Operators can override with `CPLUG_SHUTDOWN_REJECT_NEW=1|0`.

## Alternatives considered

### Use `@app.on_event("shutdown")` despite deprecation

Simplest. Works today. Emits a DeprecationWarning on every startup;
upstream rebase that promotes warnings to errors would break us.

**Rejected** — pre-emptive future-proofing is cheap (the signal
approach is ~30 LoC) and the lifespan deprecation is well-known.

### Block new requests immediately on SIGTERM (no grace)

Simple. Loses every in-flight gen, even ones with 1 sample step
remaining.

**Rejected** — graceful drain is the whole point. The orchestrator's
own grace period (k8s `terminationGracePeriodSeconds`, default 30s)
exists precisely for this.

### Coordinate via a sentinel file or shared state

For multi-replica deployments, the drain-then-handoff coordination
benefits from a shared lock. Out of scope per §1 non-goals.

**Rejected** — multi-replica coordination is a separate concern.

### Set a hard deadline kill via `os._exit` after grace

The shutdown handler doesn't actually exit the process — uvicorn /
gradio own that. Trying to force-exit from cplugapi creates ordering
hazards (uvicorn's own shutdown might already be in flight; double
exit racing the orchestrator's SIGKILL).

**Rejected** — observe-and-signal is the right separation of
concerns.

## Blast radius

- Module install path adds `RejectDuringDrainMiddleware` to
  `user_middleware`. No-op outside drain.
- SIGTERM bind is module-global; second `install()` call doesn't
  re-bind. Idempotent.
- Drain flag is shared with W1's `/readyz` sanitised public body —
  any caller of `livez_readyz.set_draining(True)` (today only the
  shutdown handler) flips the readiness probe.
- `shared.state.interrupt()` is the same call used by
  `auto_preempt`, `session_cancel`, `session_preempt`. No new
  side-effect; the interrupt is cooperative (handlers check
  `state.interrupted` and exit at their next sample step).
- Outside `/cplugapi/v1/*` and the two `/sdapi/v1/*` gen paths:
  middleware passes through unchanged. Invariant 1 byte-identity
  preserved.

## Failure modes

1. **Signal binding fails on non-main thread / Windows** — caught
   in the `try/except`, logged at DEBUG, fall back to manual
   invocation. Tests exercise the manual path directly so coverage
   isn't dependent on signal delivery.
2. **`progress.current_task` polling wedges** — bounded by the
   `time.monotonic() < deadline` check; even if the polling
   throws, the loop exits at deadline. `_has_active_work` swallows
   exceptions so a torn-down `modules.progress` (early shutdown,
   test fixture state) doesn't loop forever.
3. **Multiple SIGTERMs in rapid succession** — `graceful_shutdown`
   is idempotent. Drain flag stays set; second invocation polls,
   finds no work, exits early.
4. **Orchestrator's terminationGracePeriodSeconds < CPLUG_SHUTDOWN_GRACE_S**
   — the orchestrator wins; SIGKILL arrives before our grace
   completes. Documented in the (forthcoming) W20 cloud runbook:
   set the orchestrator grace to `CPLUG_SHUTDOWN_GRACE_S + 5`.
5. **REJECT_NEW=1 + GET /health probes** — reads pass through
   regardless of REJECT_NEW. Only POST/PUT/PATCH/DELETE under the
   targeted prefixes are rejected. Tested.

## Test surface

`tests/cplugapi/test_shutdown.py` — 19 cases:

- `graceful_shutdown` sets the drain flag immediately.
- Returns early when no active work.
- Waits + interrupts when work persists past grace.
- Mid-grace clearing exits cleanly (no interrupt fired).
- Handles missing `modules.progress` (tornado fixture) without
  raising.
- Reject middleware: passthrough when not draining; passthrough
  on desktop default; rejects POST when explicitly enabled;
  doesn't block GET; targets `/sdapi/v1/{txt2img,img2img}` but
  not `/sdapi/v1/options`; cloud profile flips REJECT_NEW default.
- W1 + W12 integration: `/readyz` reports `draining=true` on the
  public body during drain.
- Env-var resolution (default, explicit, invalid-fallback,
  truthy/falsy interpretations).
- Capability registered.
- Install is idempotent.

Full cplugapi suite: 530 passing, 4 skipped (after W7 + W8 + W9 +
W12).
