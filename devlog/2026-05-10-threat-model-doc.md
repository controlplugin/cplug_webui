# 2026-05-10 — Threat model doc (W19)

**Kind**: new doc artifact (~890 lines).
**Files**: `doc/cplugapi-threat-model.md` (new).
**Rollback**: delete the file. No code dependency.

## Symptom

The fork carries a substantial new attack surface (`/cplugapi/v1/*`,
the WebSocket session channel, the SDAPI passthrough observers) and
the security posture was distributed across the implementation devlogs
and code comments. A reviewer asking "what does this fork defend
against, and what does it explicitly *not* defend against" had no
single document to point at. For a service that may run cloud-side
(deployment profile flips to `cloud`), that's not acceptable.

## Root cause

The threat model existed implicitly across:
- `modules/cplugapi/security_middleware.py` (CSRF, Host pinning,
  origin allow-list, body limits) — partial rationale in module
  docstrings.
- `modules/cplugapi/rate_limit.py` (brute-force defence, class-based
  buckets, XFF trust) — design rationale only in the W8 devlog.
- `modules/cplugapi/idempotency.py` (replay sanitisation) — rationale
  in the W6 devlog.
- `modules/cplugapi/ws_auth.py` (WebSocket auth invariant) —
  rationale in the W2 devlog.
- `modules/cplugapi/livez_readyz.py` (public probe surface) —
  rationale in the W1 devlog.

A reader auditing the security posture had to walk all of these to
assemble the picture, and nothing covered the *gaps* (e.g. that
the fork doesn't authenticate WS messages individually, only the
upgrade).

## Decision

Single STRIDE-organised reference document under `doc/`. Structured
as:
1. **Surface inventory** — every route + WS frame, with auth class
   and trust boundary.
2. **STRIDE per surface** — Spoofing / Tampering / Repudiation /
   Information disclosure / DoS / Elevation, with concrete attack
   examples and the corresponding mitigation (or accepted gap).
3. **Trust-boundary diagram** — text-based ASCII, since this is a
   `.md` file and we don't want to commit a binary asset.
4. **Out-of-scope** — what the fork *explicitly* does not defend
   against, with rationale (single-tenant desktop assumption,
   `/sdapi/v1/*` byte-identity invariant, etc.).
5. **Known gaps with severity** — 8 items the W19 agent surfaced.
   Each entry is a tracking placeholder, not a "fix this next" list.

## Alternatives considered

1. **Inline threat model into `doc/cplugapi.md`**. Rejected — that
   doc is the API reference; folding a 900-line security analysis
   into it would bury the reference material and double the file.
2. **One file per threat (STRIDE × surface matrix)**. Rejected as
   over-engineering for a fork this size. The grouped doc reads in
   a single sitting; a per-cell layout would require a navigation
   page.
3. **Use a dedicated SAST tool (e.g. `bandit`, `semgrep`) output as
   the threat model**. Rejected — those find *code-level* issues
   (string concat in SQL, etc.), not architectural ones. The fork's
   real risks are at the design level (e.g. "WS upgrade authenticates
   the connection but not the per-message routing" — no scanner
   flags that).

## Blast radius

Doc only. Does not modify any code, test, or build artefact. No
runtime impact. Reviewers can read or ignore.

## Failure modes

- **Doc drifts away from code**: if `security_middleware.py` or
  `rate_limit.py` evolves and this doc isn't updated, the threat
  analysis becomes stale and misleading. Mitigation: each section
  links to the specific module file by name. A reviewer changing
  a module SHOULD grep `doc/cplugapi-threat-model.md` for that
  module's name before merging. We don't enforce this in CI — too
  noisy — but the convention is documented in the doc's preamble.
- **The "known gaps" list is treated as a backlog and gets stale**:
  items get fixed but stay listed; new items get found but aren't
  added. Mitigation: each gap has a status field (`accepted` /
  `tracked` / `mitigated`). When a gap is closed by code, the entry
  is updated to `mitigated` with a link to the devlog. We don't
  delete entries — historical context matters for future audits.

## Follow-up items

The W19 agent surfaced 8 specific gaps:
1. WS per-message auth (currently upgrade-only).
2. No request-body size enforcement on the WS surface itself
   (only HTTP routes; `security_middleware` is HTTP-only).
3. Idempotency cache TTL is process-local — restart wipes the
   replay window.
4. No log-redaction policy for response bodies in `cplugapi.sdapi`
   if a future op adds a body-capturing observer.
5. `X-Cplug-Intent` header has no integrity check; a client could
   mis-tag a request and confuse the upscale log (low severity).
6. Trusted-proxies list trusts the operator to configure it
   correctly; misconfiguration is silently insecure.
7. CSRF token is per-session, not per-request — token theft via
   XSS would survive until the session expires. (Out of scope:
   the fork has no XSS surface, but if extensions add one this
   matters.)
8. The capability registry is not signed; a malicious extension
   could register `auth/bypass` and the client gating logic might
   misinterpret it. (Mitigation: known-good list in the client.)

These are listed in the doc itself; not promoted to plan items yet.

## Capability registry

No new capability — purely a documentation artifact.
