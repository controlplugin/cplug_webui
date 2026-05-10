# 2026-05-10 — Idempotency replay header sanitisation (W6)

**Kind**: behaviour change in cached-response replay path.
**Files**: `modules/cplugapi/idempotency.py`,
`tests/cplugapi/test_idempotency.py`.
**Capability**: `idempotency` (unchanged).
**Rollback**: revert `_replay` to the prior deny-list behaviour
(`skip = {"content-length", "idempotency-replayed"}`) and restore the
old all-pass header copy. Test suite has the regression tests; use
those to verify rollback restored prior behaviour.

## Symptom

The idempotency replay path (`_replay` in
`modules/cplugapi/idempotency.py`) restored every cached response
header verbatim except `Content-Length` and the
`Idempotency-Replayed` marker. Cached entries therefore carried any
header the original handler set: `Set-Cookie`, `Authorization`,
`Date`, `Server`, and any future bearer/session/auth header a future
endpoint adds — all replayed on every Idempotency-Key match.

The plan-eval round flagged this as F6: defence-in-depth gap, not
an active bug today (none of cplugapi's current handlers set
`Set-Cookie` or auth tokens), but a one-rebase regression away from
real auth-token bleed across distinct callers using the same
Idempotency-Key.

## Root cause

The original implementation used a deny-list (`skip = {…}`):

```python
skip = {"content-length", HEADER_REPLAYED.lower()}
response.raw_headers = [...header for ... if name not in skip]
```

A deny-list is the wrong default for a security-relevant filter: it
only catches what the author thought to enumerate. A new endpoint
that sets `Set-Cookie` becomes a leak the day it ships.

## Decision

Replace the deny-list with an explicit allow-list:

```python
_REPLAY_ALLOW = frozenset({
    "content-type", "content-encoding", "content-language",
    "cache-control", "etag", "last-modified", "vary",
})

def _is_replay_safe_header(name):
    n = name.lower()
    if n in _REPLAY_ALLOW:
        return True
    if n.startswith("x-cplug-"):
        return True
    return False
```

The allow-list covers:

1. **Content semantics** — `Content-Type`, `Content-Encoding`,
   `Content-Language`. Required for the replayed body to decode
   correctly on the client.
2. **Cache hints** — `Cache-Control`, `ETag`, `Last-Modified`,
   `Vary`. Pure metadata, no leak vector.
3. **Fork-owned X-Cplug-* prefix** — by CLAUDE.md policy, anything
   in this namespace is fork-controlled. Replay-safe by construction;
   if a future cplugapi handler invents `X-Cplug-Cookie` (it
   shouldn't), that's a separate review point.

Anything else is dropped. A `_REPLAY_DROP` set is documented
in-source as the explicit defence-in-depth catalog
(`Set-Cookie`, `Set-Cookie2`, `Authorization`, `WWW-Authenticate`,
`Proxy-Authenticate`, `Date`, `Server`, `X-Request-Id`,
`Content-Length`, `Transfer-Encoding`, `Connection`,
`Idempotency-Replayed`) so a reader sees exactly which headers we
care about; the actual filter uses `_is_replay_safe_header` so
unknown headers default to drop.

`X-Request-Id` is on the drop list **even though** it's currently
overwritten by the `request_id` middleware on egress (verified
during plan-eval against the install order in
`router.py:_install_middlewares` and confirmed by the new
`test_middleware_install_order_request_id_outside_idempotency`
regression test). The reasoning: a future rebase that reorders
the middleware stack so request_id sits *inside* idempotency would
silently regress correlation hygiene — replays would echo the
cached id, not the current request's. Dropping the cached value
upstream of egress means we don't depend on the rebase invariant
holding.

## Alternatives considered

### Keep deny-list, add `Set-Cookie` and friends to the skip set

Minimal change, but doesn't address the root cause: the next
header-with-side-effects becomes a regression. Rejected.

### Don't cache `Set-Cookie` at all (drop on capture, not replay)

Cleaner — the cache entry never has the cookie, so the leak vector
is gone at storage time. But that loses the ability to faithfully
replay legitimate response shapes if a handler ever needs to set
something that's safe on replay (it doesn't today). Allow-list at
*replay* time is more conservative: cache stores everything
(diagnostic hooks could inspect what was set), but only replays the
safe subset.

### Per-request-class allow-lists

E.g., POST /forge/preset has a different allow-list than GET. Over-
engineered for a surface that has no current use of `Set-Cookie`.
Rejected.

## Blast radius

- Replay path now strips ~10 categories of headers that the prior
  implementation would have replayed. None of cplugapi's current
  handlers set those headers, so observed behaviour is unchanged
  for existing endpoints. Verified by the full test suite (372
  passing).
- Future endpoint adding a `Set-Cookie` to a cached response will
  see the cookie on the first call but NOT on replays. That's the
  desired behaviour; the alternative is correlation leak.
- `X-Cplug-*` headers continue to replay verbatim — fork extensions
  remain functional through replay.
- Middleware-order regression test now fails loudly if a future
  rebase moves `request_id` inside `idempotency`.

## Failure modes

1. **Handler sets a custom header outside the X-Cplug-* prefix and
   needs it on replay** — surfaces as "header missing on replayed
   response." Fix: rename the header into the X-Cplug-* namespace
   (preferred) or add it to `_REPLAY_ALLOW` with a comment
   justifying replay-safety (review-gated).
2. **Middleware install order changes** — caught by
   `test_middleware_install_order_request_id_outside_idempotency`.
3. **Replay drops `Vary` and a CDN miscaches** — `Vary` is on the
   allow-list. Tested implicitly by the cache-control test.

## Test surface

`tests/cplugapi/test_idempotency.py` adds:

- `test_replay_drops_set_cookie` — `Set-Cookie`, `Server`, and an
  arbitrary `X-Random-Trinket` header set by the handler on first
  call all dropped from the replayed response.
- `test_replay_preserves_x_cplug_prefix` — fork-owned `X-Cplug-*`
  and the static `Cache-Control` allow survive the replay.
- `test_replay_x_request_id_is_fresh_per_request` — replays carry
  the *current* request's `X-Request-Id`, not the cached one.
- `test_middleware_install_order_request_id_outside_idempotency` —
  regression-guards the canonical middleware order.

Full suite: 372 passing, 4 skipped.
