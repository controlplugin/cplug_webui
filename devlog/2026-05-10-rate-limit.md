# 2026-05-10 — Token-bucket rate limiting (W8)

**Kind**: new ASGI middleware + auth-dep wrap.
**Files**: `modules/cplugapi/rate_limit.py` (new),
`modules/cplugapi/errors.py` (added `RATE_LIMITED` code + 429 status
mapping), `tests/cplugapi/test_rate_limit.py` (new).
**Capability**: `security/rate-limit`.
**Rollback**: revert the `rate_limit.install(app)` call in
`router._install_middlewares` and the
`rate_limit.observe_auth_failures(auth_dependency)` wrap in
`setup_cplugapi`. Module + tests can stay or be removed.

## Symptom

The cplugapi surface had no rate limiting. `--api-auth` defends
against unauthorised access but not against:

- **Credential brute-force**: an attacker hammering the auth dep
  with varying passwords. Each request is cheap to reject (compare
  against the credentials dict and 401), but at thousands of req/sec
  there's still throughput cost and audit-log noise.
- **Single-credential abuse**: a legitimate-but-misbehaving client
  polling 10 Hz when the surface expected 4 Hz. With 50 routes and
  8 concurrent clients, the per-route latency degrades.
- **Read-class amplification**: `/health` and `/queue` are cheap but
  not free. A polling-loop bug in a client could amplify into
  noticeable load.

In desktop loopback the threats are minimal (single-user). In cloud
deployment they're real.

## Root cause

No rate-limit primitives in cplugapi. The plan-eval round flagged
this as F8 and elevated it to W8.

## Decision

Token-bucket rate limiting in three classes, profile-defaulted:

| Class | What | Cloud default | Desktop default |
|---|---|---:|---:|
| `mutating` | POST/PUT/PATCH/DELETE under cplugapi | 30/min/key | off |
| `read` | GET/HEAD/OPTIONS under cplugapi | 600/min/key | off |
| `auth_failed` | 401s observed via the auth wrap | 10/min/key | off |

Operators override per class via `CPLUG_RATE_LIMIT_MUTATING`,
`CPLUG_RATE_LIMIT_READ`, `CPLUG_RATE_LIMIT_AUTH_FAILED` (integer
requests-per-minute; `0` disables that class explicitly even in
cloud profile).

### Client-key resolution

Per-IP keying degenerates on loopback (every request is from
`127.0.0.1`). The plan-eval round called this out and prescribed a
per-profile keying scheme. Implementation:

- **Desktop profile**: key on `hash(Authorization header)`. Distinct
  credentials get distinct buckets; same-credential clients share a
  bucket (acceptable single-user posture). Without an Authorization
  header, key on the literal string `"auth:<none>"`.
- **Cloud profile**: key on the real client IP after parsing
  `X-Forwarded-For` (XFF). The XFF parser walks the chain
  right-to-left, skipping addresses inside `CPLUG_TRUSTED_PROXIES`
  CIDRs, until it finds the first untrusted address — that's the
  real client. If the immediate TCP peer isn't itself in the
  trusted-proxy CIDR, XFF is ignored entirely (the caller could
  have forged it).

### Cloud profile fail-fast

`validate_startup()` is called from `setup_cplugapi`. When cloud
profile is active AND any rate-limit class is enabled, it requires
`CPLUG_TRUSTED_PROXIES` to be set. Without it, the rate limit is
trivially bypassable (the caller controls XFF). Fail at startup
with a clear error rather than ship a broken-by-default rate
limiter.

### Auth-failure observability

The plan-eval flagged this as a third spike (`auth_failed` class
needs visibility into auth-dep rejections). Implementation: wrap
the auth_dependency at `setup_cplugapi` time with
`rate_limit.observe_auth_failures(auth_dependency)`. The wrap:

1. Pre-charges the auth-failed bucket *before* delegating. If the
   bucket is empty, raise 429 immediately — credential brute force
   is denied without even running the credential comparison.
2. Otherwise, delegate to the inner auth_dep. If it raises 401,
   the pre-charge counted the attempt (no double-charge).
3. If it returns successfully, the bucket has one fewer token but
   only legitimate-attempt-counted; the bucket refills at 10/min
   (cloud default) so a single legitimate user never gets
   throttled.

Keying for the auth wrap: hash of `username:password-prefix(4)`.
Distinct credential pairs get distinct buckets so an attacker
hammering user A doesn't lock out user B's bucket. The 4-char
password prefix means an attacker varying only the password hits
the same bucket — exhausting it after `auth_failed` attempts and
then 429ing — but a legitimate user with the right password gets
their own untainted bucket.

### Wire format

429 responses use the W3 problem+json envelope (`code:
"rate_limited"`), plus standard `Retry-After` (in seconds, ceiling
of refill-to-1-token). Every successful response also carries
`X-RateLimit-Limit` (capacity), `X-RateLimit-Remaining` (floored
tokens after take), `X-RateLimit-Reset` (Unix epoch seconds when
the bucket would be back to full assuming no further activity).

## Alternatives considered

### Fixed-window counter (X requests per minute, reset on minute boundary)

Simpler but worst-case 2× burst at the boundary (X requests in the
last second of minute 1, X again in the first second of minute 2).

**Rejected** — the burst is exactly the failure mode rate limiting
is supposed to prevent.

### Sliding-window log

Most accurate; stores timestamp of every request in the window. O(N)
memory per key. For 600 req/min with 1000 active keys, 600,000 entries
in flight.

**Rejected** — token bucket is a strict superset of correctness for
the use case at lower memory cost. The plan's single-replica scope
doesn't need the sliding-window guarantees.

### Per-credential keying always (no IP)

Clean for desktop; insufficient for cloud — one bad credential would
lock out all callers using that credential. Cloud's threat model
includes "many distinct attackers from many IPs all using a stolen
credential" — keying on IP isolates them.

**Rejected** — keying should be per-profile, per the spike.

### Distributed rate-limit state via Redis

Required for multi-replica deployments. Plan §1 explicitly excludes
multi-replica from scope.

**Rejected** — out of scope.

## Blast radius

- Desktop default: zero change. All classes off.
- Cloud default: 30/min mutating, 600/min read, 10/min auth_failed.
  Operators who haven't read the runbook may be surprised when the
  surface returns 429s. Mitigation: documented in the (forthcoming)
  W20 cloud deployment runbook; capability `security/rate-limit`
  surfaces on `/identify.capabilities[]` so clients can detect the
  policy.
- Cloud + no `CPLUG_TRUSTED_PROXIES`: process refuses to start
  with a clear error message naming the env var. Catches the
  misconfig at deploy time rather than after first traffic.
- Outside `/cplugapi/v1/*`: middleware passes through unchanged.
  Invariant 1 byte-identity for `/sdapi/v1/*` preserved (verified
  by `test_middleware_passes_through_outside_prefix`).

## Failure modes

1. **Token-bucket math edge case at capacity boundary** — a request
   arriving at exactly the moment the bucket has 1.0 tokens
   succeeds (the `take` checks `tokens >= 1.0`). 0.999... tokens
   would fail, but the refill is continuous so this is a vanishing
   window. Tested via `test_bucket_consumes_one_per_take`.
2. **`Retry-After` header reflects refill-to-1, not refill-to-full** —
   intentional. The client just needs to wait long enough to retry,
   not wait for the entire window to refill.
3. **Cloud profile without trusted proxies** — fail-fast at startup.
   Tested via `test_validate_startup_fails_in_cloud_with_no_trusted_proxies`.
4. **XFF spoofing from an untrusted peer** — peer-trusted check
   prevents XFF processing when the immediate TCP peer isn't in
   `CPLUG_TRUSTED_PROXIES`. Tested via
   `test_cloud_key_ignores_xff_when_peer_untrusted`.
5. **Auth-dep wrap pre-charge double-counts a legitimate user** —
   No: the legitimate user has a distinct bucket (key on
   username+password-prefix), so their bucket only counts their own
   attempts. A correct credential gets through without throttling
   the user. Tested via
   `test_observe_auth_failures_does_not_throttle_legitimate_user`.

## Test surface

`tests/cplugapi/test_rate_limit.py` — 28 cases covering:

- Bucket math (capacity, consumption, continuous refill).
- Profile-driven defaults (desktop off, cloud 30/600/10).
- Explicit env override + explicit zero.
- Class registry (disabled passes through; capacity enforces;
  distinct keys distinct buckets).
- Client-key resolution (desktop hash auth, cloud IP, XFF chain
  walking with trusted proxies, XFF ignored when peer untrusted).
- Auth-failure wrap (passes through when disabled, throttles after
  N failures, doesn't lock out distinct credentials).
- Middleware integration via setup_cplugapi (headers emitted,
  429 envelope shape, no headers when disabled, /sdapi/v1/* passthrough).
- Startup validation (desktop pass, cloud-all-disabled pass,
  cloud-with-class-enabled-no-proxies fail, cloud-with-proxies pass).
- Trusted-proxy CIDR parsing (multi-entry, invalid-entry skip).
- Capability registration.

Full cplugapi suite: 530 passing, 4 skipped (after W8 + W9 + W12).
