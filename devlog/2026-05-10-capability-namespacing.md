# 2026-05-10 — Fork-local capability namespacing (W15)

**Kind**: backward-compatible additive rename with dual emission window.
**Files**: `modules/cplugapi/capabilities.py` (added
`register_with_legacy`, `deprecated_capabilities`),
`modules/cplugapi/access_log.py`, `gen_timing.py`, `sdapi_observer.py`,
`upscale_log.py`, `livez_readyz.py` (switched to dual emit),
`modules/cplugapi/health.py`, `identify.py` (surface
`deprecated_capabilities[]`), `tests/cplugapi/test_capability_namespacing.py`
(new — 12 cases).
**Capability**: `capability-namespacing-v2` (meta).
**Rollback**: revert each fork-local module's `register_capabilities`
to call `capabilities.register(<legacy>)` directly. `health.py` and
`identify.py` can keep emitting the empty `deprecated_capabilities`
array harmlessly.

## Symptom

Six fork-local capability strings (`request-log`, `gen-timing`,
`sdapi-request-log`, `upscale-log`, `livez`, `readyz`) didn't follow
the project's slash-separated namespacing convention. Discovery is
harder for the Rust client team: "is there an observability
namespace I can grep?" — no, four observability strings live as bare
identifiers; `livez`/`readyz` live without a `health/` namespace.

## Root cause

The capability registry exists since Phase 1 (T17/T18), but the
namespacing pattern was applied piecemeal — `session/*`, `models/*`,
`forge/*` got the slash treatment, observability/health stayed
flat. The plan-eval round of `plan/cplugapi-world-class.md` flagged
this as F17 and elevated it to W15.

## Decision

Add a `register_with_legacy(new_name, legacy_name, predicate=None)`
helper. Migrating modules call it instead of `register(legacy_name)`.
Both names are simultaneously registered (dual emission); the
legacy name is tracked in a new `_deprecated` set so
`/health.deprecated_capabilities[]` and
`/identify.deprecated_capabilities[]` can publish the removal
schedule. Clients get a one-minor-release window to migrate.

### Scope (narrowed per the plan)

The plan's §1 non-goal forbids renaming **canonical** capability
strings (those listed in the project's capability registry — the
authoritative spec lives outside this repo; see the local `plan/`
directory).
W15 only touches fork-local strings — those NOT in the canonical
registry. The W15 test
(`test_canonical_strings_not_deprecated`) pins this rule by
enumerating canonical strings and asserting none surface in
`deprecated_capabilities`.

### Rename table

| Legacy (kept, deprecated) | New (preferred) |
|---|---|
| `request-log` | `observability/request-log` |
| `gen-timing` | `observability/gen-timing` |
| `sdapi-request-log` | `observability/sdapi-request-log` |
| `upscale-log` | `observability/upscale-log` |
| `livez` | `health/livez` |
| `readyz` | `health/readyz` |

New capabilities landed by W7-W12 in this session
(`observability/metrics`, `observability/log-format-json`,
`observability/trace-context-w3c`, `security/rate-limit`,
`security/per-route-body-limits`, `security/ws-auth-enforced`,
`ops/graceful-shutdown`, `error-format-problem-details`) are
already namespaced correctly — no legacy alias needed.

### Removal trigger

Per the `plan/cplugapi-world-class.md` §3.1 deprecation policy:
- Minimum one minor release of dual emission (≥ 30 days).
- Rust client team confirms migration in writing (PR comment on the
  OpenAPI artifact diff in W18's release flow).
- Removal lands in the next minor; not a major bump (cplugapi v1
  is internal-client-only).

Time-window-elapsed alone does NOT trigger removal — the Rust
client confirmation is the load-bearing gate.

## Alternatives considered

### Hard rename (drop legacy in same release)

Simpler. Cuts the dual-emission complexity entirely. Breaks every
Rust client that pinned to the old strings.

**Rejected** — the Rust client codegen is frozen against the
current strings; a hard rename without a migration window is a
forced upgrade.

### Mid-string deprecation marker (e.g. `request-log-deprecated`)

Encode the deprecation in the string itself. Easy to grep.

**Rejected** — the whole point is for the new client to use the
new name. A "-deprecated" suffix would just create a third string
to clean up.

### Separate `DEPRECATED_CAPABILITIES[]` env var

Operators opt-in to deprecation warnings. Cuts the wire-side
overhead.

**Rejected** — the surface is the contract; clients should detect
deprecation via the wire response, not via a side-channel.

### Per-string predicate-driven deprecation

Each legacy string registered with a predicate that always returns
True but logs a warning on every `/health` hit. Surfaces in logs;
keeps the wire shape clean.

**Rejected** — log noise on every health check defeats the purpose;
operators want a quiet steady state and explicit signals.

## Blast radius

- `/health.capabilities[]` and `/identify.capabilities[]` now include
  BOTH the new namespaced strings AND the legacy flat strings for
  six modules. Existing clients that switch on the legacy strings
  keep working; new clients that switch on the namespaced strings
  also work.
- New `deprecated_capabilities[]` array on both endpoints lists the
  legacy strings. Clients that consume it can detect the migration
  window without polling release notes.
- No middleware behaviour changes. No new env vars. No new endpoints.
- /sdapi/v1/* byte-identity unaffected.

## Failure modes

1. **Client switches on legacy string after removal lands** — gets
   "capability not present" from their code's perspective. Mitigation:
   `deprecated_capabilities[]` gives them advance notice; the OpenAPI
   diff in the release pipeline (W18) shows the schema change.
2. **`_deprecated` set leaks between tests** — `capabilities.reset()`
   clears both the registry and the deprecated set. Verified by
   `test_reset_clears_deprecated_set`.
3. **Operator monitors via flat names in dashboards** —
   `deprecated_capabilities[]` surfaces the heads-up; operator can
   update dashboards before removal.
4. **A future contributor uses `register_with_legacy` for a
   canonical string** — the test `test_canonical_strings_not_deprecated`
   fails CI, flagging the misuse. Mitigation by-test, not by-code
   (the registry has no way to know which strings are "canonical" —
   that's an external policy).

## Test surface

`tests/cplugapi/test_capability_namespacing.py` — 12 cases:

- `register_with_legacy` emits both names; legacy goes into
  deprecated set.
- `unregister(legacy)` drops from deprecated set.
- `reset()` clears the deprecated set.
- Each fork-local module (access_log, gen_timing, sdapi_observer,
  upscale_log, livez_readyz) emits the namespaced new name + legacy
  alias when registered.
- Canonical strings (identify, session/cancel, etc.) are NOT
  flagged as deprecated.
- `/health` and `/identify` surface `deprecated_capabilities[]`.
- Every entry in `deprecated_capabilities` is also in `capabilities`
  (dual emission invariant).
- `deprecated_capabilities` is sorted for stable diffs.

Full cplugapi suite: 546 passing, 4 skipped.
