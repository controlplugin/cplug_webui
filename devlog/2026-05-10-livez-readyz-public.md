# 2026-05-10 — `/livez` and `/readyz` move to the public router (W1)

**Kind**: routing change + body-shape sanitisation.
**Files**: `modules/cplugapi/router.py`, `modules/cplugapi/livez_readyz.py`,
`tests/cplugapi/test_livez_readyz.py`, `tests/cplugapi/test_no_sdapi_regression.py`.
**Capability**: `livez`, `readyz` (unchanged).
**Rollback**: revert the `livez_readyz.attach(public, ...)` call in
`router.py` to `attach(private)` and drop the `verbose` query handling
in `livez_readyz.py`.

## Symptom

When `--api-auth` is configured, k8s liveness/readiness probes return
401 because the `/livez` and `/readyz` routes were on the cplugapi
private router (which inherits the same Basic-auth dependency as
`/sdapi/v1/*`). K8s probes don't typically inject `Authorization`
headers — orchestrators expect probe endpoints to be unauthenticated
or to use a separate auth surface. This defeats the entire point of
having standardised probe endpoints: a cloud-deployed cplugapi
backend behind `--api-auth` was effectively unprobeable without
configuring credential-injection in the k8s manifest, which most
charts don't support cleanly.

## Root cause

`router.py:_do_mount` originally attached `livez_readyz` to the
private sub-router:

```python
private = APIRouter()
...
livez_readyz.attach(private)
...
app.include_router(private, prefix=PREFIX, dependencies=[Depends(auth_dependency)])
```

The `Depends(auth_dependency)` wrapper applied to the entire private
router gates every route under it — `/livez` and `/readyz` included.
There was no way to expose them publicly without also exposing the
diagnostic detail their bodies carried (`last_error.detail`, full
checkpoint paths, GPU memory).

## Alternatives considered

### Option A — split into two endpoints

`/readyz` (public, sanitised) and `/readyz/verbose` (private, full
detail). Clean from a FastAPI point of view; two routes with two
distinct dependency profiles.

**Rejected** because the URL surface is what the OpenAPI codegen
freezes. Adding a sibling `/readyz/verbose` introduces a path the
Rust client codegen would have to track separately and the doc would
have to explain alongside the primary `/readyz`. A single `/readyz`
with a query-param toggle keeps the surface flat.

### Option B — public only, drop `last_error.detail` entirely

Remove the diagnostic detail across the board; never expose `kind`,
`detail`, `recorded_at` over HTTP. Operators read it from logs.

**Rejected** — the diagnostic block is genuinely useful for an
authenticated operator running `curl /readyz?verbose=1` during a
triage session. Forcing them to grep logs is a UX regression for the
desktop-loopback target audience (single-user, full trust).

### Option C — keep on private; document the k8s probe pattern

"Configure your k8s probes to inject Basic auth via a secret-volumed
file." Real, but most charts don't support it cleanly and it's
surprising that a probe endpoint requires credentials.

**Rejected** — invariant 4 (cplugapi inherits `--api-auth`) doesn't
forbid public routes within the prefix; `/identify` is already
public for the same reason. K8s-style probes are exactly the kind of
endpoint where being public is correct.

## Decision

`livez_readyz.attach()` now takes `auth_dependency` as a keyword arg
and is mounted on the public router. The handler signature for
`/readyz` carries a `verbose: bool = Query(False)` parameter:

- `verbose=False` (default, public): returns booleans only —
  `{torch_importable, model_loaded, has_error, draining}`. No error
  detail, no checkpoint path. Safe to expose to a third party.
- `verbose=True`: returns the full diagnostic body —
  `{torch_importable, model_loaded, last_error, draining}` where
  `last_error` is the full `{kind, detail, recorded_at}` record.
  Requires Basic auth when `auth_dependency` is configured.

The verbose-mode auth check manually parses the `Authorization`
header and re-invokes the same `auth_dependency` callable that gates
the private router. FastAPI's `Depends()` machinery doesn't support
"depends conditional on a query param at route declaration time", so
the check is in the handler body. ~20 LoC; the dependency is not
duplicated, just re-entered.

The `draining` flag is exposed on the public body unconditionally —
operational state, not a leak vector. K8s probes need to observe it
unauthenticated to pull the pod from rotation during a graceful
shutdown (W12, where `set_draining(True)` is called from the
shutdown handler).

## Blast radius

`/livez` body is unchanged: `{"status": "live"}`.

`/readyz` body has changed shape for the **default** (non-verbose)
case:

- **Before**: `{status, checks: {torch_importable, model_loaded, last_error}}`.
  Status code 200/503. Caller had to authenticate to receive this.
- **After (default)**: `{status, checks: {torch_importable, model_loaded, has_error, draining}}`.
  Status code 200/503. No auth needed.
- **After (?verbose=1)**: `{status, checks: {torch_importable, model_loaded, last_error, draining}}`.
  Same as the old body modulo the new `draining` field. Auth required
  when `--api-auth` set.

The Rust client doesn't currently consume `last_error` (verified —
client treats /readyz as a boolean status probe). The body-shape
change is therefore safe; clients that DO need diagnostic detail
must switch to `?verbose=1` and authenticate.

## Failure modes

1. **Operator forgets `?verbose=1` and assumes the absence of
   `last_error` means "no error has been recorded"** — `has_error:
   true` is present in that case, so the operator sees the signal
   even without the detail. The fix is to add `?verbose=1` to the
   curl invocation, not to misinterpret the body.
2. **Verbose-mode auth check rejects a previously-working caller**
   — when `--api-auth` is configured, `?verbose=1` now requires
   credentials. A monitoring system that hit `/readyz?verbose=1`
   without auth before W1 would have been 401-ing already because
   the route was private; W1 doesn't make this worse.
3. **K8s probe scrapes the body for `model_loaded == true`** —
   still present in the public body. No regression.
4. **Manual `Authorization: Basic <garbage>` header** —
   `_parse_basic_credentials` raises 401 with `WWW-Authenticate:
   Basic`, same as `HTTPBasic(auto_error=True)` would.

## Test surface

`tests/cplugapi/test_livez_readyz.py` adds:

- `test_probes_work_unauth_when_api_auth_set` — both probes return
  200/503 without credentials when auth is configured.
- `test_readyz_verbose_requires_auth_when_api_auth_set` — `?verbose=1`
  returns 401 without credentials.
- `test_readyz_verbose_with_valid_creds` — verbose body includes the
  full `last_error` record and omits `has_error`.
- `test_readyz_verbose_unrestricted_when_no_api_auth` — without auth
  configured, verbose is open (desktop-loopback posture).
- `test_readyz_draining_flag_visible_unauth` — drain state observable
  on the public body for k8s rotation.
- `test_readyz_verbose_invalid_basic_header` — garbage Authorization
  → 401, not 500.

`test_no_sdapi_regression.py::test_only_identify_is_unauthenticated`
updated: the public surface is now `[/identify, /livez, /readyz]`,
not just `[/identify]`.

Full suite (354 cplugapi tests) passes locally.
