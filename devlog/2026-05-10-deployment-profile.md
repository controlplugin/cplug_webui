# 2026-05-10 — `CPLUG_DEPLOYMENT_PROFILE` env var (W5)

**Kind**: new env var + profile-driven defaults across multiple modules.
**Files**: `modules/cplugapi/profile.py` (new),
`modules/cplugapi/security_middleware.py`,
`modules/cplugapi/auto_preempt.py`,
`modules/cplugapi/router.py`,
`tests/cplugapi/test_deployment_profile.py` (new).
**Capability**: `deployment-profile-cloud` (only when active).
**Rollback**: unset `CPLUG_DEPLOYMENT_PROFILE` (defaults to
`desktop`); revert `profile.py` and the read-sites (security
middleware constructor, auto_preempt mode resolver).

## Symptom

The fork ships with desktop-loopback defaults baked into every
module that reads env vars: `CPLUG_ALLOWED_HOSTS=127.0.0.1,localhost,[::1]`,
`CPLUG_PREEMPT_MODE=always`. Cloud deployment requires overriding all
of them via individual env vars before the surface is even reachable
behind an ingress (default ALLOWED_HOSTS rejects every cloud
hostname). Operators have to discover these one at a time, often
after the first 403.

## Root cause

No central concept of "deployment posture". Each module made the
loopback assumption locally — correct for the primary use case
(desktop ControlPlugin client), wrong for the secondary cloud
deployment that the §1 plan-eval round elevated to a supported (if
secondary) target.

## Decision

Introduce `CPLUG_DEPLOYMENT_PROFILE=desktop|cloud` as a single switch
that flips a coordinated set of defaults. Profile is read once at
install time via `profile.is_cloud()` / `profile.is_desktop()`.

### Profile-driven knob table

| Knob | `desktop` (default) | `cloud` |
|---|---|---|
| `ALLOWED_HOSTS` default | `127.0.0.1, localhost, [::1]` | `*` (any non-empty Host) |
| `ALLOWED_ORIGINS` default | loopback regex | `*` (any non-empty Origin) |
| `auto_preempt` mode default | `always` | `off` |
| `rate-limit` defaults (W8 hook) | all classes off | mutating=30/min, read=600/min, auth_failed=10/min |

The wildcard `*` is a new sentinel in the security middleware's
allow-list. When present, the corresponding check returns "allow"
for any non-empty value. Operators who want this without a profile
can set `CPLUG_ALLOWED_HOSTS=*` directly.

Important invariant: **explicit env vars always win over profile
defaults.** If the operator sets `CPLUG_ALLOWED_HOSTS=api.example.com`
under cloud profile, the wildcard does NOT additionally apply —
they got exactly what they asked for. Tested.

Sec-Fetch-Site checks are NOT profile-gated: cloud profile sets
wildcard Origin but `Sec-Fetch-Site: cross-site` still rejects
because that's the *actual* cross-origin gate. The Origin allow-list
in the loopback model exists because Sec-Fetch-Site doesn't always
fire (some clients omit it); in cloud, the ingress is doing the
upstream filtering anyway, so wildcard-Origin-plus-Sec-Fetch-Site is
the right mix.

### Auto-preempt cloud default

The plan draft initially proposed `header` mode in cloud, then
eval'd to `off`. Reasoning: cloud deployments are not the
sketch-workflow target. Preempt-by-default is correct ONLY for the
desktop client where every gen is a stale-stroke event; in cloud,
operators pay for compute and don't want the framework cancelling
their gens. Operators who actually want preempt in cloud opt in via
`CPLUG_PREEMPT_MODE=always` or `=header`.

## Alternatives considered

### One env var per knob, no profile

Status quo. Operators set 4+ env vars to cloud-deploy. Discoverability
problem; runbook needed.

**Rejected** — discoverability is exactly what the profile solves. The
runbook (W20) still helps but is no longer required for first-boot.

### Profile in a config file (TOML/YAML)

Cleaner, but the fork's current convention is env vars (see
`webui-user.bat`). Adding a config file ahead of W13's centralised
config module would create two surfaces. Defer to W13.

**Rejected** — same reason as above.

### `CPLUG_DEPLOYMENT_PROFILE=k8s` as a third profile

K8s deployments are a special case of cloud (probes work without
auth — already handled by W1). No knob differs between "generic
cloud" and "k8s cloud" today.

**Rejected** — YAGNI. Add when a knob actually differs.

## Blast radius

- Default desktop behaviour: zero change. `desktop` is the default
  and matches every prior default.
- Cloud profile, no other env vars: `Host: anything-non-empty` is
  accepted; `Origin: anything` is accepted; auto_preempt is off.
  This is the intended cloud default.
- Mixed (cloud profile + explicit `CPLUG_ALLOWED_HOSTS=...`): the
  explicit env var wins. Operator gets exactly the host allow-list
  they configured.
- Capability `deployment-profile-cloud` surfaces on `/health.capabilities[]`
  and on `/identify.capabilities[]` (W4) only when cloud is active.
  Clients can detect the deployment posture remotely and adjust
  expectations.

## Failure modes

1. **Operator typos `CPLUG_DEPLOYMENT_PROFILE=cloud_v2`** —
   `_resolve_profile` warns and falls back to `desktop`. Surfaces
   in the boot log.
2. **Operator sets cloud profile but keeps `CPLUG_ALLOWED_HOSTS`
   pointing at a loopback** — explicit override wins, behaviour
   stays loopback-only, surface unreachable from the cloud
   hostname. Operator-error category; no automatic rescue.
3. **Cloud profile + explicit `CPLUG_PREEMPT_MODE=always`** —
   honours the explicit value (preempt fires per request). Tested.

## Test surface

`tests/cplugapi/test_deployment_profile.py` (15 cases):

- `profile.get_profile()` defaults, explicit values, unknown fallback.
- `profile.register_capabilities()` only emits cloud capability when
  active.
- security_middleware: cloud profile accepts arbitrary Host /
  Origin; desktop profile rejects.
- security_middleware: explicit env wins over profile.
- security_middleware: cloud profile still rejects
  `Sec-Fetch-Site: cross-site`.
- auto_preempt: desktop default `always`; cloud default `off`;
  explicit env wins.
- /identify: `deployment-profile-cloud` surfaces in cloud, absent
  in desktop.

Full cplugapi suite: 393 passing, 4 skipped.
