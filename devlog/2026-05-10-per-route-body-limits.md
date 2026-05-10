# 2026-05-10 — Per-route body-size caps in security middleware (W7)

**Kind**: middleware extension + new env var.
**Files**: `modules/cplugapi/security_middleware.py`,
`tests/cplugapi/test_security_middleware.py`.
**Capability**: `security/per-route-body-limits`.
**Rollback**: revert the `ROUTE_LIMITS` table, the
`_parse_route_limits_env` / `_match_route_limit` helpers, the
`route_body_limits` constructor arg, and the per-route branch in
`_check_body_size`. Drop the new capability registration. The global
32 MiB cap returns to being the only body limit.

## Symptom

The global `CPLUG_MAX_BODY_BYTES` cap defaults to 32 MiB — sized
correctly for the *largest* legitimate cplugapi payload (a base64
mask delivered to `canvas/strokes`). For the small-input endpoints
(`/forge/preset/{name}`, `/session/cancel/{id_task}`,
`/session/preempt`) that cap is wildly oversized: legitimate input
fits in <1 KiB. An authenticated client (or anyone past `--api-auth`)
could POST 32 MiB of JSON garbage to those endpoints and waste
upstream parsing/validation budget before the 422 fires. Auth doesn't
help here — the threat is noisy-neighbour or compromised-credential
abuse, not unauthenticated DoS.

## Root cause

Body-size caps in the security middleware were a single global
threshold. The middleware checks `Content-Length > self._max_body_bytes`
and rejects with 413; there was no path-aware variant.

## Alternatives considered

### Per-route Pydantic `Field(..., max_length=N)` constraints

Add per-field length validators on the request models for each tiny
endpoint. Pydantic catches the violation as a `ValidationError`, the
W3 problem-details handler converts to 422.

**Rejected.** The validator fires *after* the JSON body is fully
buffered and parsed — that's the exact work we want to skip when a
32 MiB payload arrives at a 4 KiB endpoint. The body-size guard has
to run pre-parse, on `Content-Length`, in the security middleware
layer.

### Single global cap, lowered

Drop the global cap to 4 KiB. Safe for the small endpoints, breaks
`canvas/strokes` immediately.

**Rejected.** The endpoint with the largest legitimate payload sets
the floor; you can't lower below it without breaking the legitimate
case.

### Length-aware per-handler decorator

Decorator on each route function that asserts request size at the
top of the handler. Same problem as the Pydantic option — fires
after body buffering.

**Rejected.** Same root reason; the pre-parse property is the whole
point of catching this in the security middleware.

### Pure-ASGI middleware reading the message stream

Wrap the receive callable, count bytes as they flow, abort on the
first byte past the cap. More accurate than `Content-Length` (catches
chunked-without-CL, catches lying clients), but a chunk of work and
adds to the streaming surface that already coexists awkwardly with
gradio's long-poll endpoints.

**Rejected for now.** The desktop client always sends `Content-Length`;
loopback uvicorn rejects chunked-without-CL with 411 already. If a
threat model emerges where lying clients matter, revisit.

## Decision

Add a `ROUTE_LIMITS: dict[(method, prefix), bytes]` table beside the
existing global cap. The matcher predicate is **longest-prefix
terminated by `/` or end-of-string**:

- A rule prefix matches the path when the path either equals the
  prefix exactly (EOS branch), the prefix already ends in `/` (any
  continuation is a fresh segment), or the next character past the
  prefix is `/`.
- When multiple rules match, the longest prefix wins.
- Method must match exactly.

This boundary rule is the load-bearing detail: `/forge/preset/sketch`
matches the `/forge/preset/` rule; `/forge/preset-bulk` does NOT
(boundary char is `-`, neither `/` nor EOS) and falls back to the
global cap. Adjacent paths cannot accidentally inherit a sibling's
cap — important for forward-compat as new routes land that share a
prefix with an existing strict-cap route.

Operators override via new env var `CPLUG_ROUTE_BODY_LIMITS`, parsed
as CSV of `METHOD:path:bytes`. Empty/unset uses the built-in
`ROUTE_LIMITS`. Setting the env var **replaces** the defaults rather
than merging with them — same posture as `CPLUG_ALLOWED_HOSTS`. An
operator who wants to override one route while keeping the rest must
list every route they want capped.

The 413 envelope reuses `CODES.BODY_TOO_LARGE` (no new code) so client
switch-on-code logic doesn't fragment. The detail string distinguishes
the two paths:

- Global: `request body too large: {size} > {global_cap}`.
- Route: `request body too large: {size} > {route_cap} (route-specific limit: {route_cap} bytes)`.

Human-readable distinction; machine-readable code is identical
(intentional — clients don't care which cap fired, only that the
body was too big).

## Blast radius

Three concrete routes get tighter caps:

| Route                              | Before  | After   |
|------------------------------------|---------|---------|
| `POST /cplugapi/v1/forge/preset/*` | 32 MiB  | 4 KiB   |
| `POST /cplugapi/v1/session/cancel/*` | 32 MiB | 4 KiB   |
| `POST /cplugapi/v1/session/preempt` | 32 MiB | 4 KiB   |

Every other cplugapi POST/PUT/PATCH still gets the 32 MiB global cap.
The desktop client's actual payloads on these routes are well under
4 KiB (preset names are short strings; cancel takes a UUID; preempt
takes a small JSON struct), so production behaviour does not change
under any expected workload.

`/sdapi/v1/*` is untouched (per the path-scope guard at the top of
`dispatch`). Other middleware behaviour (origin, host, fetch-site)
unchanged.

## Failure modes

1. **Operator sets `CPLUG_ROUTE_BODY_LIMITS` with a typo on one
   entry** — that entry is logged at WARNING and dropped; valid
   entries on the same line still apply. Tested.
2. **Operator sets the env var listing only one route** — the other
   built-in caps disappear (no implicit merge). Documented in the
   helper docstring; runbook (W20) will spell this out. The
   alternative — silent merge — would surprise the opposite way:
   "I set this to a single permissive value to disable a default
   and the default still applies".
3. **A new cplugapi route lands that shares a prefix with a
   capped route but means something different** (e.g. a
   `/forge/preset-bulk` actually exists) — the boundary rule
   correctly does NOT apply the cap. The new route gets the global
   32 MiB cap until someone explicitly registers it. Tested with
   the adjacent-path case.
4. **Client sends `Content-Length` larger than the cap but the
   actual body is small** — middleware rejects on the declared
   length, not the actual. Same posture as the global cap; clients
   that lie about `Content-Length` are misbehaving anyway.
5. **Route matches multiple rules** (operator-induced via env) —
   longest prefix wins. Ties are not possible because two distinct
   prefixes of the same length cannot both prefix the same path.

## Test surface

`tests/cplugapi/test_security_middleware.py` — 12 new cases:

- `_match_route_limit` unit tests:
  - Trailing-slash prefix matches an extension segment.
  - EOS branch matches an exact-path rule (`/session/preempt`).
  - Adjacent path (`/forge/preset-bulk`) does NOT match the
    `/forge/preset/` rule.
  - Method must match.
  - Longest matching prefix wins.
- End-to-end via direct middleware dispatch:
  - 64 KiB POST to `/forge/preset/sketch` → 413 with
    `application/problem+json`, code `body_too_large`, detail
    contains `"route-specific"`.
  - 1 KiB POST to the same path → passes the size check.
  - 5 KiB POST to `/forge/preset-bulk` (adjacent, no rule) → passes
    (falls through to 32 MiB global cap).
- Env override:
  - `CPLUG_ROUTE_BODY_LIMITS=POST:/cplugapi/v1/_test/strict:512`
    rejects 1024 bytes, accepts 256 bytes.
  - Env replaces (not merges with) defaults: `/forge/preset/sketch`
    accepts 64 KiB when not listed in the override.
  - Malformed entries are skipped; valid entries on the same line
    still parse.
- Problem envelope carries `request_id` from the inbound
  `X-Request-Id` header (same envelope contract as the global cap).
- Capability registration test asserts
  `security/per-route-body-limits` is registered alongside the
  existing security capabilities.

Full cplugapi suite green: 435 passed, 4 skipped (was 404 passed,
4 skipped pre-W7; +12 new tests, no regressions).
