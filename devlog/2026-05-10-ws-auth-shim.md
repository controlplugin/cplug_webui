# 2026-05-10 — WebSocket auth invariant shim (W2)

**Kind**: new ASGI middleware enforcing a hard invariant ahead of
endpoints that need it.
**Files**: `modules/cplugapi/ws_auth.py` (new),
`modules/cplugapi/router.py` (wire-up),
`tests/cplugapi/test_ws_auth.py` (new).
**Capability**: `security/ws-auth-enforced`.
**Rollback**: revert `ws_auth.install(...)` call in
`router._install_middlewares` and drop the module + tests. The
fork's invariant 4 lapses back to "documented but unenforced"
(matching the state before this change).

## Symptom

`CLAUDE.md` invariant 4 and `doc/cplugapi.md` both claimed
"`/cplugapi/v1/*` (HTTP and WebSocket upgrade) honors the same
`--api-auth` Basic auth as `/sdapi/v1/*`." The HTTP half is enforced
by the router's private/public split; the WebSocket half had
**no enforcement code**. There were no WS endpoints in the fork
yet, so the claim was vacuous-but-future-load-bearing — a Track 05
T31 (`/session/stream/{id_task}`) implementation that forgot to
inherit the auth dependency would silently regress the invariant
without any test catching it.

## Root cause

WebSockets in FastAPI/Starlette use a different dependency-injection
mechanism than HTTP routes. The cplugapi private router applies
`Depends(auth_dependency)` to every HTTP route under it via
`app.include_router(private, dependencies=[...])`, but that
mechanism doesn't fire on WebSocket scopes — those go through
`@app.websocket(...)` registration which wires dependencies
per-route. There was no policy gate at the prefix level for WS
upgrades.

The plan-eval round of `plan/cplugapi-world-class.md` flagged the
draft solution (option (a): delete the doc claim, defer enforcement
to T31) as insufficient — a doc edit cannot enforce an invariant.
Option (b) — pre-implement the gate — was elevated from "future
T31 work" to a Phase WA blocker.

## Decision

`modules/cplugapi/ws_auth.py` exports a pure-ASGI middleware
(`CplugapiWsAuthShim`) installed at the front of
`app.user_middleware` so it runs outermost. The shim:

1. No-op for HTTP scopes — `if scope["type"] != "websocket": pass through`.
2. No-op for WS paths outside `/cplugapi/v1/` — invariant 1
   (byte-identity for `/sdapi/v1/*` upstream) preserved.
3. No-op when `auth_dependency=None` (no `--api-auth` configured) —
   matches HTTP surface posture: without auth, the surface is open.
4. Otherwise: parse the `Authorization` header from the ASGI scope
   bytes; if missing or non-Basic-or-malformed, reject. If Basic
   present, invoke `auth_dependency(HTTPBasicCredentials(...))` —
   the same callable that gates the HTTP private router. If it
   raises, reject.

Rejections emit HTTP 403 with the W3 problem+json envelope via the
ASGI `websocket.http.response.*` events:

```python
{"type": "websocket.http.response.start", "status": 403, "headers": [...]}
{"type": "websocket.http.response.body", "body": <problem+json>}
```

Modern uvicorn translates this to a real HTTP 403 response on the
upgrade socket. The client sees a proper credential-failure error
rather than an opaque close code.

The two rejection codes (`auth_required` for missing/malformed
Authorization, `auth_failed` for valid-shape-but-invalid creds)
match the existing HTTP error catalog and let the client
distinguish "I forgot to send creds" from "my creds are wrong".

### Forward-checked test pattern

The tests don't rely on `TestClient.websocket_connect` (which has
proven environment-fragile in this Python 3.13 + starlette 0.47.1
+ httpx 0.27.2 stack — bare WS without any cplugapi setup also
fails in our CI venv). Instead, the shim's `__call__` is invoked
directly with mock `scope`/`receive`/`send` coroutines. This tests
the actual ASGI contract the shim implements — what messages it
emits, when it forwards to the inner app, what credentials it
parses — without depending on an end-to-end transport.

The advantage: if T31 (or any other contributor) later adds a real
WS endpoint and forgets to inherit the auth dep, the shim **still
fires first** because it's at the front of `user_middleware`. The
new endpoint can't bypass the gate; the test suite already covers
the policy.

## Alternatives considered

### Wait for T31 to land WS endpoints, add enforcement then

Status quo before W2. The doc claim stays accurate-ish in the
"will be enforced when relevant" sense, but invariant 4 is at
T31's mercy.

**Rejected** — cost of pre-implementation is ~50 LoC + ~150 LoC
test, far less than the rebase risk if T31 gets the auth wiring
wrong and ships before someone notices.

### Subclass `WebSocketRoute` and require auth at registration

FastAPI / Starlette let you customise route classes. We could
subclass `WebSocketRoute` such that registration without
`dependencies=[Depends(auth_dependency)]` raises at app boot.

**Rejected** — depends on contributors using the right route
class. The middleware approach intercepts every WS upgrade
regardless of how the route was registered, which is the right
default for a hard invariant.

### Reject by closing with code 1008 (Policy Violation)

Simpler than the `websocket.http.response.*` flow; works in every
ASGI server.

**Considered, but rejected**: a 1008 close gives the client a
WebSocket close, not an HTTP rejection. Browsers and the Rust
client both prefer to see an HTTP-level 403 before the upgrade
completes — clearer error UX, matches the HTTP surface's behaviour
on the same auth failure. Modern uvicorn supports the
`websocket.http.response.*` flow; if the dependency on it ever
proves problematic, the fallback to 1008 close is a 5-line change.

## Blast radius

- New WebSocket upgrades under `/cplugapi/v1/*`: gated by Basic
  auth when `--api-auth` is set; rejected with 403 + problem+json
  body otherwise. Today there are no such endpoints, so observed
  behaviour is unchanged.
- New WebSocket upgrades outside `/cplugapi/v1/*`: zero change.
  Verified by `test_ws_outside_cplugapi_prefix_falls_through`.
- HTTP traffic: zero change. Shim short-circuits on `scope["type"]
  != "websocket"`.
- When T31 lands and adds the first real WS endpoint
  (`/session/stream/{id_task}`), the shim will already be enforcing
  the invariant. T31's own auth wiring becomes redundant
  (defense-in-depth) but the shim is the load-bearing layer.

## Failure modes

1. **Uvicorn version too old to support `websocket.http.response.*`**
   — not encountered in this project, but the shim would silently
   fail to send the close. Fix: switch to `websocket.close` with
   code 1008 (5-line change). Document the dep on uvicorn ≥ 0.16
   (which supports the flow) in `pyproject.toml` if it becomes a
   real issue.
2. **`auth_dependency` raises a non-`HTTPException`** — the shim
   catches `Exception` broadly and rejects with `auth_failed`. Log
   line surfaces the underlying exception name; if a contributor
   accidentally returns a coroutine from `auth_dep` instead of
   awaiting (rare; the FastAPI `auth` is sync), the shim rejects
   the upgrade rather than passing through.
3. **Future bypass through a sub-application mounted under
   `/cplugapi/v1/`** — `app.mount("/cplugapi/v1/sub", sub_app)`
   would attach a separate router. The shim is on the parent app's
   middleware stack, so the upgrade enters through the parent
   first; it should still fire. Tested by exercising the path-prefix
   logic directly, but a regression test specifically against
   sub-app mounting would belong in T31's tests.

## Test surface

`tests/cplugapi/test_ws_auth.py` (11 cases) covers:

- HTTP scope falls through unchanged.
- WS outside `/cplugapi/v1/` falls through unchanged.
- WS under `/cplugapi/v1/` with no auth configured falls through.
- WS under `/cplugapi/v1/` with auth configured + missing
  Authorization → 403 + `auth_required`.
- Malformed Basic header → 403, auth_dependency NOT called.
- Non-Basic scheme (Bearer) → 403.
- Valid Basic shape but invalid credentials (auth_dependency
  rejects) → 403 + `auth_failed`, auth_dependency IS called.
- Valid credentials → inner app reached, no rejection messages.
- Capability registration.
- Install is idempotent.
- `setup_cplugapi(app, auth_dependency=...)` threads the dep
  through to the shim.

Full cplugapi suite: 404 passing, 4 skipped.
