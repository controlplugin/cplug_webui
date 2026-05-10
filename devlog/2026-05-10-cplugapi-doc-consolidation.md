# 2026-05-10 — cplugapi doc + CHANGES consolidation (W14, W17, W21, W22)

**Kind**: documentation-only edits to existing files.
**Files**: `doc/cplugapi.md` (modified, ~422 line delta),
`CHANGES.md` (modified, ~238 line delta).
**Rollback**: revert the diffs. No code dependency.

## Symptom

The Phase WA + WB code work (W1–W12) landed feature-by-feature, with
each feature's rationale in its own devlog. Two readers were left
without a clear entry point:

- The **API consumer** (desktop client maintainer, third-party
  integrator) needs a single reference doc that describes every
  `/cplugapi/v1/*` route, every env var, every capability string,
  the error envelope shape, and the middleware contract. The W1–W12
  work added 8+ such concerns; without consolidated reference text,
  the consumer's only resource was reading source.
- The **change consumer** (someone reading a release diff before
  upgrading) needs a `CHANGES.md` entry covering the entire WA/WB/WC
  wave so they know what shifted in one read.

## Root cause

Devlogs are *engineering* documentation — they explain why a change
exists. They are not consumer-facing documentation. The reference
material was missing.

## Decision

Three doc additions and one log:

1. **W14 — middleware pattern section** (`doc/cplugapi.md`,
   "Middleware patterns: when to use which"). Documents the
   `BaseHTTPMiddleware` vs pure-ASGI choice, with the Starlette#1438
   streaming-response footgun as the canonical reason to reach for
   pure-ASGI. Reference for future fork maintainers adding new
   middleware.
2. **W17 — error code catalog** (`doc/cplugapi.md`, "Error code
   catalog"). Stable error-code table mapping each `code` field
   value to its HTTP status, semantic meaning, and which module
   raises it. Client maintainers can switch on these without grepping
   `modules/cplugapi/errors.py`.
3. **W21 — operational reference additions** (`doc/cplugapi.md`,
   multiple sections):
   - Deployment profile section (`CPLUG_DEPLOYMENT_PROFILE` flag,
     what each value changes).
   - Curl examples for every public surface (`/livez`, `/readyz`,
     `/health`, `/identify`, `/version`, `/queue`, `/session/*`,
     `/forge/preset`, `/models/*`).
   - OpenAPI artifact section pointing at the W18 GitHub Release
     asset.
   - livez/readyz section updated for the W1 public-posture change.
4. **W22 — CHANGES.md WA-WC entry**. One section under "World-class
   hardening (Phase WA-WC, W1-W18)" summarising the wave. Reader
   pulling a release diff sees one heading instead of 18.

## Alternatives considered

1. **One devlog per WD-doc item** (W14, W17, W21, W22 each get
   their own). Rejected as ceremony — these are all edits to two
   files (`cplugapi.md` and `CHANGES.md`), they all landed in one
   pass, and the per-item rationale is short enough to fit in one
   doc. Splitting would produce four ~30-line files that all say
   "added a section". CLAUDE.md's "one file per change set"
   convention applies here: this was one change set.
2. **Skip the consolidation devlog entirely** (per CLAUDE.md's
   "too small to justify an entry" exception). Rejected because
   the doc additions are substantial (~660 line net delta), they
   change consumer-visible reference material, and the W14/W17/W21
   numbering deserves a paper trail when someone audits the plan
   later.

## Blast radius

Doc only. No code, no tests, no build artefact. A misread by a
consumer could cause them to hit a 4xx (e.g. if the curl example
is mistyped), but no runtime impact on the server.

## Failure modes

- **The doc drifts from the runtime**: if `errors.py` adds a new
  `Code` and the catalog table isn't updated, clients can't switch
  on it. Mitigation: the catalog notes "Codes are appended over
  time, never renamed" in the prose; the runtime is the
  source of truth via `/cplugapi/v1/openapi.json`, which lists
  every code in the response schema for the relevant routes.
- **The middleware-pattern section becomes a "law" that prevents
  future contributors from using `BaseHTTPMiddleware` when it would
  in fact be the right choice**: the section is framed as "when to
  use which" with explicit criteria, not as a blanket "always use
  pure-ASGI". A future contributor evaluating the criteria can
  still pick `BaseHTTPMiddleware` if their middleware doesn't
  interact with streaming responses.
- **Curl examples leak local-environment paths** (CLAUDE.md ban).
  Mitigation: every curl example uses `$API` (placeholder env
  var) and `$AUTH` for credentials, never an absolute URL or a
  real credential. Diff was scanned for `D:/`, `127.0.0.1`,
  `localhost` (the last appears only in the placeholder env var
  setup, not in any committed real URL).

## Follow-up items

None promoted from this consolidation. The W19 and W20 docs landed
as separate artifacts (`cplugapi-threat-model.md`,
`cplugapi-cloud-deploy.md`) with their own devlogs.

## Capability registry

No new capability — purely a documentation pass.
