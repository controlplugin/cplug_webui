# 2026-05-10 — `/identify` exposes `capabilities[]` (W4)

**Kind**: response-shape addition (additive — no field removed).
**Files**: `modules/cplugapi/identify.py`,
`tests/cplugapi/test_identify.py` (new),
`tests/cplugapi/test_router.py` (one assertion).
**Capability**: none added (the change is to `/identify` itself).
**Rollback**: revert `identify.py` to drop the `capabilities` field
from the response. No other surface changes.

## Symptom

`/identify` is the only unauthenticated cplugapi endpoint, intended
for backend-fingerprinting before a client decides whether to send
credentials. But the response carried only fork/upstream identity —
not capability information. A client that wanted to know "does this
build support `session/preempt`?" had to authenticate first to read
`/health.capabilities[]`.

For the desktop ControlPlugin, this means the bootstrap flow
("am I talking to a cplug-enabled fork? if so, which features?")
required two roundtrips and credential exchange before the capability
question was answered. For cloud deployments, monitoring tools that
poll `/identify` to confirm "yes, fork is up, supports feature X"
couldn't do so without credential injection.

## Root cause

`/identify` predates the capability-discovery use case in the
client's bootstrap flow. The original spec (Track 05 §5.1) framed it
as identity-only.

## Decision

Add `capabilities: list[str]` to `/identify`'s response. The list is
the same set returned by `capabilities.enabled_capabilities()`,
filtered through `_safe_capability()` to guard against accidental
leak of deployment specifics:

- Reject anything matching `^[a-f0-9]{7,40}$` (looks like a git SHA).
- Reject anything ending in `.safetensors`, `.ckpt`, `.pt`, `.pth`,
  `.bin`, `.gguf` (looks like a checkpoint filename).

Today no registered capability matches either filter — the
capability registry already rejects dot notation at `register()`
time, so a `*.safetensors` capability would fail registration. The
filter is defence-in-depth: it catches a future capability that
slips past the registry (via direct `_registry` injection from an
extension or a future bug), and it documents the policy that
public capability names must be deployment-agnostic identifiers.

## Alternatives considered

### Add `capabilities[]` unfiltered

Simpler. Today no capability is sensitive, so the filter currently
adds zero value.

**Rejected** — the cost of the filter is tiny (~10 LoC); the
opportunity for it to save us from a future leak is real (a future
"checkpoint-loaded/<name>" capability would be a natural fit for
the registry but a leak on `/identify`). Better to encode the
policy now than to retrofit it after a CVE.

### Allow-list specific capabilities, deny the rest

Strictest posture: only known-safe capability names appear on
`/identify`; everything else is private until the operator opts
into public exposure.

**Rejected** — too restrictive for the bootstrap-discovery use
case. Most fork capabilities ARE public-safe; an allow-list would
force every new capability through a docs+code update before it
could be used by the client at bootstrap time. The deny-pattern
filter strikes the right balance: default allow, except for known
leak shapes.

### Expose only a fixed `bootstrap_capabilities[]` array, not the
### full `enabled_capabilities()` set

Would let us hand-curate exactly which capabilities surface
publicly. Doubles the maintenance surface (every capability needs
a public/private decision in code).

**Rejected** — same reason as the allow-list option: the default
should be "public", with deny-patterns for the leak shapes we know
about.

## Blast radius

- `/identify` response gains a new key (`capabilities`). Existing
  clients that decode JSON ignore unknown keys; no breaking change.
- Capability registry behaviour unchanged. `/health.capabilities[]`
  still returns the full set post-auth.
- The Rust desktop client's bootstrap flow can now do
  capability-conditional behaviour on the first `/identify` request
  instead of waiting for the post-auth `/health` roundtrip. Behaviour
  unchanged until the client opts into it.

## Failure modes

1. **A future capability legitimately needs to embed a hash** (e.g.
   `build/<sha>` to expose the build commit) — caught by the
   `_LOOKS_LIKE_HASH` filter and silently dropped. Fix: rename the
   capability to a non-hash form (e.g. `build-info` and put the SHA
   in a separate header / handler response). The filter prefers a
   loud "your capability didn't show up" symptom over a quiet leak.
2. **An extension registers a capability via direct `_registry`
   injection** that contains a leak shape — filtered out at
   `/identify`, still visible on `/health`. Tested by
   `test_identify_filters_unsafe_string_at_egress`.

## Test surface

`tests/cplugapi/test_identify.py` (new, 7 cases):
- `test_identify_returns_capabilities_list` — list-of-strings shape.
- `test_identify_capabilities_surface_without_auth` — works without
  credentials when `--api-auth` is configured.
- `test_safe_capability_filters_hex_shas` — short and full SHAs
  rejected.
- `test_safe_capability_filters_checkpoint_suffixes` — model file
  extensions rejected.
- `test_identify_filters_unsafe_string_at_egress` — defence-in-depth:
  bypass the registry validation, confirm the egress filter strips
  the leak.
- `test_identify_capabilities_sorted` — stable output for diffing.
- `test_identify_returns_constants` updated in `test_router.py` to
  also assert `capabilities` is present.

Full cplugapi suite: 378 passing, 4 skipped.
