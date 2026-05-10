# 2026-05-10 — Cloud deployment doc (W20)

**Kind**: new doc artifact (~894 lines).
**Files**: `doc/cplugapi-cloud-deploy.md` (new).
**Rollback**: delete the file. No code dependency.

## Symptom

The fork has a `CPLUG_DEPLOYMENT_PROFILE=cloud` flag (W5) that flips
several defaults (rate limit on, host allow-list strict, auto-preempt
off, reject-on-drain on, etc.), but how to actually deploy in that
mode — what env vars to set, what reverse proxy config to write,
what k8s probes to wire — lived only as scattered hints in module
docstrings. An operator wanting to put this behind a load balancer
on cloud infrastructure had no end-to-end recipe.

## Root cause

`profile.py` documented the *flag* but not the *deployment
operations*. The capability strings (`health/livez`, `health/readyz`,
`security/rate-limit`, `error-format-problem-details`) gave clients
a way to detect features but didn't tell operators how to wire them
into infra.

## Decision

Single operator-facing doc covering:
1. **Profile flip** — what `CPLUG_DEPLOYMENT_PROFILE=cloud` actually
   changes (table of every default that moves).
2. **Required env vars in cloud mode** — `CPLUG_ALLOWED_HOSTS`,
   `CPLUG_ALLOWED_ORIGINS`, `CPLUG_TRUSTED_PROXIES`, `--api-auth`
   (always required when not loopback).
3. **Reverse-proxy templates** — nginx and Caddy snippets, with
   the WS upgrade rules and the `X-Forwarded-For` chain
   configured to match what `rate_limit.py` expects.
4. **k8s manifest skeleton** — Deployment, Service, ConfigMap,
   probes wired to `/cplugapi/v1/livez` (liveness) and
   `/cplugapi/v1/readyz` (readiness), with `terminationGracePeriodSeconds`
   tuned to the W12 drain budget (`CPLUG_SHUTDOWN_DRAIN_S`).
5. **Observability wiring** — `/cplugapi/v1/metrics` Prometheus
   scrape config, `cplugapi.access` log routing via the W9 JSON
   formatter, trace propagation via the W11 `traceparent` echo.
6. **Test plan** — 10 specific behaviours an operator should verify
   in their target environment before declaring deployment healthy
   (auth challenge, rate limit response shape, drain behavior on
   SIGTERM, etc.).

## Alternatives considered

1. **Helm chart instead of a doc**. Rejected at this stage: the
   fork is single-instance by design (no clustering, no shared
   idempotency cache), so a chart would imply more deployment
   sophistication than the actual code supports. Doc + manifest
   snippets convey "this works as a single replica with these
   knobs" without overpromising distributed correctness.
2. **Per-cloud-provider sub-docs** (AWS, GCP, fly.io, etc.).
   Rejected — most of the recipe is provider-agnostic (env vars,
   reverse-proxy, k8s). The provider-specific bits (managed
   load balancers, secret stores) are short and don't justify
   doc fan-out.
3. **Generate the manifest from the capability registry**
   programmatically (e.g. emit probe paths from `capabilities.py`
   directly). Rejected — clever, but the registry is a Python
   runtime artifact; generating YAML from it requires bootstrapping
   the whole webui to render the doc. Hand-maintained snippets are
   simpler and the surface is small (5 paths).

## Blast radius

Doc only. Does not modify any code, test, or build artefact. No
runtime impact.

## Failure modes

- **Manifest snippets drift away from the runtime**: e.g. if W12's
  default drain budget changes from 30s and this doc still says
  `terminationGracePeriodSeconds: 35`, an operator copy-pastes a
  stale value. Mitigation: every env var in the manifest examples
  has a comment naming the module-level default — a reviewer can
  cross-check with the module without re-reading this doc.
- **Reverse-proxy templates fall out of date with `security_middleware.py`**:
  e.g. if we add a new mandatory header check, the nginx snippet
  doesn't auto-update. Mitigation: the doc cross-references
  `security_middleware.py` by section and lists every header the
  middleware inspects, so an operator can diff against their proxy
  config when troubleshooting.
- **An operator follows the doc but skips the test plan**: the
  test plan exists specifically because the failure modes above
  produce *silent* misconfigurations (e.g. `X-Forwarded-For` chain
  set up wrong → rate limit keys on the proxy IP, not the client).
  Mitigation: the doc opens with a "do not skip the test plan"
  preamble, and each test in the plan names the symptom you'll see
  if you skipped it.

## Follow-up items

The W20 agent identified 10 specific behaviours that should be
covered by integration tests but aren't yet:
1. Drain → `/readyz` returns 503 within 100ms of SIGTERM.
2. Drain → in-flight requests still drain to completion (don't get
   rejected mid-handler).
3. Rate-limit key derived from the right XFF position with
   trusted-proxies set.
4. Rate-limit key falls back to remote_addr when XFF is unparseable.
5. Auth challenge response shape is RFC 9457 problem+json.
6. WebSocket upgrade respects `--api-auth` (covered by W2 unit
   tests, but no integration test in a real-proxy setup).
7. Probe surfaces 200 even when `--api-auth` is required (W1
   invariant — covered by unit tests but not integration).
8. SIGTERM during a long-running gen — drain waits for the gen, then
   exits.
9. Metrics endpoint scrape format is parseable by `promtool check`.
10. JSON logs are parseable by `jq` end-to-end (no embedded raw
    newlines or unescaped quotes).

These are listed in the doc itself; not promoted to plan items yet.
They'd land as a `tests/cplugapi/integration/` directory if the
fork ever grows a real-proxy CI environment.

## Capability registry

No new capability — purely a documentation artifact.
