# cplugapi threat model

This is the security audit primer for the `/cplugapi/v1/*` surface. It
catalogues the threats the fork defends against, the mitigations wired
into the middleware stack, and the threats explicitly out of scope. It
complements `doc/cplugapi.md` (the reference doc) and the
mitigation-bearing source files (`modules/cplugapi/security_middleware.py`,
`rate_limit.py`, `ws_auth.py`, `idempotency.py`, `livez_readyz.py`,
`identify.py`, `errors.py`, `tracing.py`, `shutdown.py`).

The fork has two supported deployment shapes; everything below is
keyed against them.

- **Desktop (primary).** The desktop ControlPlugin client speaks to a
  fork process bound on `127.0.0.1:7860`. Single user. Same-machine
  trust model. `--api-auth` is optional but recommended.
- **Cloud single-replica (secondary).** Operator-paid GPU behind an
  ingress that terminates TLS plus stronger auth (OIDC / JWT) and
  forwards to the fork bound on `0.0.0.0` inside the cluster. The
  fork still runs `--api-auth` against a generated service-account
  credential the ingress rewrites the upstream auth into.

Multi-replica deployments are explicitly out of scope per
`plan/cplugapi-world-class.md` §1: distributed rate-limit state,
sticky-session routing for idempotency replay, and cross-replica
cancellation are non-goals.

## 1. Trust boundaries

### 1.1 Loopback bind (desktop)

```
+-----------------------------+      +-----------------------+
|  Same-OS user processes     |      |  Browser-resident JS  |
|  (Photoshop, ControlPlugin, |      |  (any tab the artist  |
|   curl, IDE, terminals)     |      |   has open)           |
+-------------+---------------+      +-----------+-----------+
              |                                  |
              |       loopback (lo / 127.0.0.1)  |
              v                                  v
       +-----------------------------------------------------+
       |  cplug_webui process (uvicorn + FastAPI)            |
       |  bound 127.0.0.1:7860                               |
       |   /sdapi/v1/*       (byte-identical to upstream)    |
       |   /cplugapi/v1/*    (this surface)                  |
       +-----------------------------------------------------+
```

Inside the trust boundary:

- The fork process and its in-process extensions / Gradio modules.
  These run with the same OS privileges as the user; they are NOT
  defended against (see §4).
- Any process running as the same OS user. Loopback is a same-user
  channel — process isolation is the kernel's job, not ours.

Outside the trust boundary, despite being on the same machine:

- **Browser JavaScript** running in any tab the artist visits. JS can
  fetch `http://127.0.0.1:7860/cplugapi/v1/...`, and if the artist's
  password manager has previously cached `--api-auth` credentials
  for that host, the browser will replay them. This is the live
  threat the security middleware exists to defend against (CSRF /
  cross-origin abuse, DNS rebinding).
- **Remote network traffic.** The bind is loopback; no external
  packets reach the socket. The only path in from the internet is
  via a compromised browser or DNS-rebind attack.

### 1.2 Cloud ingress (cloud)

```
+----------------+     TLS     +-----------------------+    HTTP    +----------+
|  Internet      | ----------> |  Ingress / WAF        | ---------> |  fork    |
|  client        |             |  (TLS termination,    |            |  0.0.0.0 |
|  (browser,     |             |   OIDC/JWT validation,|            |          |
|   Rust desktop |             |   rate-limit option,  |            |          |
|   over WAN)    |             |   --api-auth rewrite) |            |          |
+----------------+             +-----------------------+            +----------+
                                            ^                            ^
                                            |                            |
                            trust boundary --+    private cluster net    |
                                                  CPLUG_TRUSTED_PROXIES  +
```

Inside the trust boundary:

- The fork process and the ingress / sidecar in front of it.
- The cluster network the ingress routes through. The fork
  trusts XFF only when the immediate TCP peer is in
  `CPLUG_TRUSTED_PROXIES`; without that, the rate-limit keying
  reverts to the raw peer (`modules/cplugapi/rate_limit.py:295-339`).

Outside the trust boundary:

- Public internet. TLS at the ingress is the bulk-data channel
  protector; the fork itself sees plain HTTP.
- Any caller that can spoof XFF without going through a trusted
  proxy. The peer-trust check stops the spoof from reaching the
  XFF parser.

The ingress is responsible for:

- TLS termination (the fork does NOT run TLS itself).
- Public auth (OIDC / SAML / JWT — whatever the operator has).
- Optional WAF (request shape, body size, IP reputation).
- Auth rewrite — strip the public credential, inject the
  fork's `--api-auth` Basic credential before forwarding.

The fork is responsible for:

- Honouring `--api-auth` on every cplugapi route except the
  three intentionally public ones (`/identify`, `/livez`, `/readyz`).
- Running the cloud rate-limit defaults.
- Refusing to start when `CPLUG_TRUSTED_PROXIES` is unset and
  rate-limit classes are enabled (`rate_limit.validate_startup()`).

## 2. Adversary classes

The mitigations below are sized against five concrete adversaries.

### A. Browser-resident cross-origin attacker (loopback profile)

Lives in any tab the artist has open. Goal: land an
unauthorised mutating request on the cplugapi surface using the
artist's cached `--api-auth` credentials.

Vectors:

- Cross-origin `fetch(...)` to `http://127.0.0.1:7860/cplugapi/v1/...`.
  The browser includes Basic credentials cached for that origin.
- `<form>` POST to a cplugapi endpoint. CORS doesn't gate forms,
  but the body shape is restricted (typically `application/x-www-form-urlencoded`).
- DNS rebinding: register `evil.example`, ship JS that fetches
  `http://evil.example:7860/cplugapi/v1/...`, re-bind the DNS
  record to `127.0.0.1` between resolution attempts. The browser
  considers the origin `evil.example` (so SOP "protects" the
  attacker), but the TCP packet hits the local backend.

Mitigations: §3 rows 1, 2, 3.

### B. Local-system non-cplugapi-aware process (loopback profile)

Any other process on the same OS, running as the same user.

Out of scope. See §4. The OS isolation layer (user separation,
sandboxing, namespace) is the right place to defend; the fork
trusts same-user processes by construction.

### C. Remote attacker reaching the loopback bind

Has no direct path. Reaches the surface only via:

- A compromised browser belonging to the artist (adversary A).
- A DNS-rebind attack against the artist's browser (adversary A).
- A compromised same-machine process (adversary B, out of scope).

The fork's mitigations against adversary A cover the remote case
implicitly.

### D. Cloud-side credential brute-force / token-stuffing attacker

Lives on the public internet. Goal: guess the `--api-auth`
credential by hammering authentication-required endpoints.

Vectors:

- Valid HTTP `POST` to `/cplugapi/v1/health` with an invalid
  Basic header, looped from a botnet.
- Stuffing: try a username/password pair leaked from another
  service.
- Target the WS upgrade specifically (W2 ws_auth shim treats it
  identically to HTTP from a credential-failure perspective).

Mitigations: §3 rows 4, 8.

### E. Cooperative-but-misbehaving client

The desktop client has a polling-loop bug, a too-aggressive retry
policy, or oversize uploads. Not malicious; just noisy.

Vectors:

- `/health` polled at 10 Hz instead of 4 Hz.
- `/forge/preset/{name}` with 32 MiB of garbage in the body
  (the client somehow attached the wrong payload).
- Stuck in a credential-typo loop, generating 401s every ~50 ms.

Mitigations: §3 rows 4, 5, 6.

## 3. Threats addressed and how

The middleware install order (per `plan/cplugapi-world-class.md` §3.0
and enforced by `tests/cplugapi/test_idempotency.py::test_middleware_install_order_request_id_outside_idempotency`)
is:

```
ws_auth -> rate_limit -> tracing -> request_id ->
shutdown -> idempotency -> security_middleware -> access_log -> handler
```

Each threat below names the layer that handles it and the test that
guards the regression.

| # | Threat | Mitigation | Capability | Env var | Test |
|---|---|---|---|---|---|
| 1 | CSRF / cross-origin browser fetch | `Origin` allow-list + `Sec-Fetch-Site` reject of `cross-site` / `same-site` (`security_middleware.py:398-448`) | `security/origin-checks` | `CPLUG_ALLOWED_ORIGINS` | `tests/cplugapi/test_security_middleware.py` (Origin / Sec-Fetch-Site cases) |
| 2 | DNS rebinding | Strict `Host` allow-list, exact match against loopback names (`security_middleware.py:450-477`) | `security/host-checks` | `CPLUG_ALLOWED_HOSTS` | `tests/cplugapi/test_security_middleware.py` (Host cases) |
| 3 | Body-size DoS / zip bomb (global) | 32 MiB `Content-Length` cap, pre-parse (`security_middleware.py:479-534`) | `security/body-size-cap` | `CPLUG_MAX_BODY_BYTES` | `tests/cplugapi/test_security_middleware.py::test_post_body_*` |
| 3a | Body-size DoS on small endpoints | Per-route caps: 4 KiB on `/forge/preset/`, `/session/cancel/`, `/session/preempt` (`security_middleware.py:122-126`) | `security/per-route-body-limits` | `CPLUG_ROUTE_BODY_LIMITS` | `tests/cplugapi/test_security_middleware.py` (W7 cases) |
| 4 | Credential brute force | `auth_failed` token bucket, 10/min/credential-key in cloud profile, peek-then-charge to deny cheaply (`rate_limit.py:390-487`) | `security/rate-limit` | `CPLUG_RATE_LIMIT_AUTH_FAILED` | `tests/cplugapi/test_rate_limit.py` (auth-failure wrap cases) |
| 5 | Read amplification / polling DoS | `read` token bucket, 600/min/key in cloud profile (`rate_limit.py:91-95`) | `security/rate-limit` | `CPLUG_RATE_LIMIT_READ` | `tests/cplugapi/test_rate_limit.py` (read class cases) |
| 5a | Mutating-request flood | `mutating` token bucket, 30/min/key in cloud profile (`rate_limit.py:91-95`) | `security/rate-limit` | `CPLUG_RATE_LIMIT_MUTATING` | `tests/cplugapi/test_rate_limit.py` (mutating class cases) |
| 6 | Cookie / auth replay across distinct callers via Idempotency-Key | Replay allow-list — only `Content-Type`, `Content-Encoding`, `Cache-Control`, `ETag`, `Last-Modified`, `Vary`, and `X-Cplug-*` survive replay (`idempotency.py`, devlog 2026-05-10-idempotency-replay-sanitize) | `idempotency` | `CPLUG_IDEMPOTENCY_MAX`, `CPLUG_IDEMPOTENCY_TTL_S` | `tests/cplugapi/test_idempotency.py::test_replay_drops_set_cookie`, `test_replay_x_request_id_is_fresh_per_request` |
| 7 | Stale request-id log correlation | `X-Request-Id` is on the replay drop list; install order keeps `request_id` outside `idempotency` so egress always stamps a fresh id | `idempotency` (defence-in-depth) | n/a | `test_middleware_install_order_request_id_outside_idempotency` |
| 8 | WebSocket bypass of HTTP auth | Pure-ASGI shim parses Basic from the upgrade scope BEFORE the handler binds, rejects 403 with problem+json (`ws_auth.py`) | `security/ws-auth-enforced` | n/a (inherits `--api-auth`) | `tests/cplugapi/test_ws_auth.py` |
| 9 | Information leak via `/readyz` body | Sanitised public body — booleans only (`livez_readyz.py:189-202`); `?verbose=1` is auth-gated when `--api-auth` is set | `health/readyz`, legacy `readyz` | n/a | `tests/cplugapi/test_livez_readyz.py::test_readyz_verbose_requires_auth_when_api_auth_set` |
| 10 | Information leak via `/identify` capabilities | `_safe_capability` egress filter strips strings matching `^[a-f0-9]{7,40}$` or ending in `.safetensors`/`.ckpt`/`.pt`/`.pth`/`.bin`/`.gguf` (`identify.py:42-58`) | `identify` | n/a | `tests/cplugapi/test_identify.py::test_identify_filters_unsafe_string_at_egress` |
| 11 | Drain-time abuse during shutdown | Optional `RejectDuringDrainMiddleware` returns 503 on POST to gen entry points while drain flag is set (cloud default; opt-in on desktop); k8s probes pull the pod from rotation before completion (`shutdown.py`) | `ops/graceful-shutdown` | `CPLUG_SHUTDOWN_REJECT_NEW`, `CPLUG_SHUTDOWN_GRACE_S` | `tests/cplugapi/test_shutdown.py` (reject-during-drain cases) |
| 12 | Trace context spoofing | `tracing.py:_validate_traceparent` rejects malformed / all-zero ids and silently mints a fresh `traceparent`; outbound is always the canonical value (`tracing.py`) | `observability/trace-context-w3c` | n/a | `tests/cplugapi/test_tracing.py::test_inbound_*_replaced` |
| 13 | Generic HTTP exception leaking internals | RFC 9457 problem+json envelope inside `/cplugapi/v1/*`; FastAPI default `{detail}` outside (preserves byte-identity); detail strings curated, no stack traces echoed (`errors.py`) | `error-format-problem-details` | n/a | `tests/cplugapi/test_errors.py` |
| 14 | Diagnostic log flood (operator-side DoS) | `CPLUG_ACCESS_LOG`, `CPLUG_SDAPI_OBSERVER`, `CPLUG_GEN_TIMING` default to `0` in `webui-user.bat` | `observability/request-log`, `observability/gen-timing`, `observability/sdapi-request-log` | per-stream env vars | `tests/cplugapi/test_access_log.py`, `test_gen_timing.py` (toggle behaviour) |
| 15 | Path-confusion sibling-route inheritance | Per-route body-limit matcher uses longest-prefix-terminated-by-`/`-or-EOS rule so `/forge/preset-bulk` does NOT inherit the `/forge/preset/` cap (`security_middleware.py:215-256`) | `security/per-route-body-limits` | `CPLUG_ROUTE_BODY_LIMITS` | `tests/cplugapi/test_security_middleware.py` (adjacent-path test) |
| 16 | XFF spoofing in cloud profile | XFF parser walks right-to-left skipping trusted-proxy CIDRs; falls back to peer if peer is not trusted (`rate_limit.py:295-339`) | `security/rate-limit` | `CPLUG_TRUSTED_PROXIES` | `tests/cplugapi/test_rate_limit.py::test_cloud_key_ignores_xff_when_peer_untrusted` |
| 17 | Cloud rate-limit bypass via missing trusted proxies | Fail-fast at startup: cloud + any rate-limit class enabled + no `CPLUG_TRUSTED_PROXIES` raises `RuntimeError` (`rate_limit.py:610-646`) | `security/rate-limit` | `CPLUG_TRUSTED_PROXIES` | `tests/cplugapi/test_rate_limit.py::test_validate_startup_fails_in_cloud_with_no_trusted_proxies` |

### 3.1 Detail on the load-bearing mitigations

#### Origin / Sec-Fetch-Site (#1)

The check has three accept paths and one reject path.

- Accept: header absent (native client, Tauri / Electron with
  `Origin` stripped, curl, server-to-server).
- Accept: `Origin: null` (file:// pages, sandboxed iframes — the
  desktop-companion threat model trusts these because the browser
  writes the literal `null`; cross-origin pages can't forge it).
- Accept: regex match against
  `^http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$` (the
  loopback regex; `https` to a loopback bind is unusual enough that
  the few legitimate users opt in via `CPLUG_ALLOWED_ORIGINS`).
- Reject: anything else, with 403 `origin_not_allowed`.

`Sec-Fetch-Site: cross-site` and `Sec-Fetch-Site: same-site` are
rejected; `none` and `same-origin` pass; absence is treated as
legacy / native and allowed. The Sec-Fetch-Site check is **not**
profile-gated (cloud profile keeps it active) — in cloud, the
ingress filters the obvious cross-origin attacker, but the
defence-in-depth cost is zero and the cross-site case still has no
legitimate path.

The two checks are intentionally redundant. Some clients omit
`Sec-Fetch-Site`; some clients strip `Origin`. Both have to fail
before a hostile request gets through.

#### Host allow-list (#2)

Exact match. The substring trap (`127.0.0.1.evil.example`
trivially passes a substring check) is why the regex is anchored
on both ends and the allow-list is exact. Bare names
(`127.0.0.1`) and host:port (`127.0.0.1:7860`) variants both
pass. Wildcard (`*`) accepts any non-empty Host — used by the
cloud profile because the ingress controls vhost routing.

#### Body-size caps (#3, #3a)

Pre-parse, on `Content-Length`. Reads the header alone — never
buffers the body. The 32 MiB global is sized for the largest
legitimate payload (a base64 mask delivered to `canvas/strokes`).

The per-route caps (W7) tighten three high-risk small-input
endpoints to 4 KiB. The matcher predicate is "longest-prefix
terminated by `/` or end-of-string"; this is load-bearing for
forward-compat (`/forge/preset-bulk` correctly does NOT inherit the
`/forge/preset/` cap because the boundary char is `-`, not `/` /
EOS).

Operators override via `CPLUG_ROUTE_BODY_LIMITS=METHOD:path:bytes,...`.
Empty / unset uses the defaults; setting the env var **replaces**
the defaults entirely (no implicit merge — same posture as
`CPLUG_ALLOWED_HOSTS`).

We do not stream-count bytes. The desktop client always sends
`Content-Length`; loopback uvicorn rejects chunked-without-CL with
411 already. If a threat emerges where lying clients matter,
revisit (devlog 2026-05-10-per-route-body-limits §"Pure-ASGI
middleware reading the message stream").

#### Credential brute force (#4)

Token-bucket bucket, 10/min/credential-key default in cloud
profile, off in desktop. Keying is **username only** (devlog
2026-05-10-rate-limit, plus inline note in `rate_limit.py:441-456`).
An earlier draft keyed on `username + password-prefix(4)`; review
caught that an attacker varying the password past character 4 gets
a fresh bucket per attempt, defeating the point. Username-only is
correct: the legit user produces no 401s on their own bucket; an
attacker brute-forcing username U is throttled regardless of which
password they try; different usernames get different buckets so
attacks against one user don't lock out others.

The wrap is *peek-then-charge*: peek is non-consuming. If the
bucket is empty, refuse 429 immediately without delegating to the
inner credential check (so a flood of failed credentials gets
rejected without running the credential comparator). If the bucket
has tokens, delegate; charge only when the inner raises 401.

#### Idempotency replay sanitisation (#6)

Allow-list, not deny-list (devlog 2026-05-10-idempotency-replay-sanitize).
The original deny-list missed any header the next contributor
adds; the explicit allow-list is the right default.

`_REPLAY_ALLOW = {"content-type", "content-encoding", "content-language",
"cache-control", "etag", "last-modified", "vary"}` plus
prefix-allow `x-cplug-*`. Anything else is dropped — including
`Set-Cookie`, `Set-Cookie2`, `Authorization`, `WWW-Authenticate`,
`Proxy-Authenticate`, `Date`, `Server`, `X-Request-Id`,
`Content-Length`, `Transfer-Encoding`, `Connection`,
`Idempotency-Replayed`. The drop list is documented in-source as
the explicit defence-in-depth catalog so a reader sees exactly
which headers we care about.

`X-Request-Id` is on the drop list **even though** the canonical
install order keeps `request_id` outside `idempotency` (so it
overwrites on egress). The drop is a hedge against a future rebase
that reorders the stack — the regression test
`test_middleware_install_order_request_id_outside_idempotency`
fails loudly if the order shifts.

#### WS auth shim (#8)

Pure-ASGI middleware that runs outermost. Sees the raw WebSocket
upgrade scope before any handler binds. Parses the Basic header
from the scope bytes; if missing or non-Basic-or-malformed, rejects
403 with the W3 problem+json envelope via the
`websocket.http.response.*` ASGI events.

Forward-checked: there are no production WS endpoints today
(`T31` `/session/stream/{id_task}` is Phase 2 work). The shim is
load-bearing for the day T31 lands — its registration path can't
bypass the gate because the shim is on the parent app's middleware
stack, not the route declaration.

The shim treats the same `auth_dependency` callable as the HTTP
private router. There's no second auth layer to keep in sync; one
config point, two transports.

#### `/readyz` sanitisation (#9)

W1 moved `/livez` and `/readyz` to the public router so k8s probes
work without credential injection. The default response body is
booleans only:

```json
{
  "status": "ready" | "not_ready",
  "checks": {
    "torch_importable": true,
    "model_loaded": true,
    "has_error": false,
    "draining": false
  }
}
```

`?verbose=1` lifts the sanitisation but requires Basic auth when
`--api-auth` is configured (`livez_readyz.py:284-289` re-enters the
same auth dependency manually). Without `--api-auth`, verbose is
open — desktop-loopback posture.

The `draining` flag is on the public body unconditionally
(operational state, not a leak vector). K8s probes need to observe
it unauthenticated to pull the pod from rotation during W12's
graceful shutdown.

#### `/identify` capability filter (#10)

`_safe_capability` rejects strings matching `^[a-f0-9]{7,40}$`
(any plausible git SHA, short or full) or ending in
`.safetensors`, `.ckpt`, `.pt`, `.pth`, `.bin`, `.gguf` (any
plausible checkpoint file extension).

Today no registered capability matches either filter — the
capability registry already rejects dot notation at `register()`
time, so a `*.safetensors` capability would fail registration. The
filter is defence-in-depth: it catches a future capability that
slips past the registry (via direct `_registry` injection from an
extension or a future bug), and it documents the policy that
public capability names must be deployment-agnostic identifiers.

The filter applies only at `/identify`. The full registry is still
visible on the post-auth `/health.capabilities[]`, which is the
right balance — bootstrap discovery without leak surface, full
detail post-auth.

#### Reject during drain (#11)

`RejectDuringDrainMiddleware` is opt-in (default desktop = off,
default cloud = on; override via `CPLUG_SHUTDOWN_REJECT_NEW`).
While the drain flag is set:

- POSTs to `/cplugapi/v1/*` return 503 with `Retry-After: 5`.
- POSTs to `/sdapi/v1/{txt2img,img2img}` (the gen entry points)
  return 503.
- All other paths pass through — `/sdapi/v1/options`,
  `/sdapi/v1/models`, `/sdapi/v1/progress`, `/cplugapi/v1/health`,
  `/livez`, `/readyz` etc. stay reachable so probes and metadata
  reads continue serving during drain.

This is a deliberate exception to the `/sdapi/v1/*` byte-identity
invariant for the two gen routes. Documented here, not in
CLAUDE.md (the invariant is unconditional in CLAUDE.md; the
exception is profile-gated and operationally justified — see §8).

#### Trace context (#12)

The W3C `traceparent` validator rejects malformed inputs (shape
regex, hex, segment lengths) and the all-zero `trace-id` /
`parent-id` cases that W3C §3.2.2.5 / §3.3.2.5 explicitly forbid.
Failed validation silently mints a fresh server-side traceparent
(`secrets.token_hex` for the random fields, `version=00`,
`flags=00`); the request continues and the outbound `traceparent`
header carries the fresh value.

The "silently replace" failure mode is intentional — the alternative
(reject 400 on malformed trace context) would let an attacker break
unrelated traffic by injecting a single bad header. The wire ends
up inconsistent (the upstream proxy's expected context is dropped),
but the request still completes and downstream span correlation
restarts cleanly.

#### Problem+json envelope (#13)

RFC 9457 problem+json shape, `code` for stable machine-switch,
`request_id` for log correlation. Detail strings are curated — no
stack traces, no internal exception messages, no file paths.

The handler defers to FastAPI's default `{detail: ...}` body for
any path outside `/cplugapi/v1/`. Invariant 1 (byte-identity for
`/sdapi/v1/*`) is preserved; verified by
`tests/cplugapi/test_errors.py::test_httpexception_outside_cplugapi_uses_default_handler`.

## 4. Threats explicitly NOT addressed

These are out of scope. Defence-in-depth at the application layer
is the wrong layer for them; another part of the stack owns the
defence.

### 4.1 Local-system attacker post-auth

Same-user processes have full access to the fork's process memory,
file descriptors, and network sockets through OS facilities the
fork doesn't control. The OS is the right layer to defend (user
isolation, sandboxes, container namespaces); the fork's threat
model assumes same-user trust.

### 4.2 Malicious WebUI extension

WebUI extensions run in-process. They get full access to the
FastAPI app, the Gradio UI, and the fork's middleware stack. The
trust model assumes operators install only extensions they have
vetted. This is the same posture as upstream Forge Neo / A1111;
the fork doesn't introduce a second policy.

### 4.3 Kernel-level interception

A rootkit or compromised kernel can sniff loopback traffic,
inject syscalls, or modify the fork's binary on disk. Out of
scope — kernel integrity is the OS's responsibility.

### 4.4 Multi-replica / distributed attacks

Per `plan/cplugapi-world-class.md` §1 non-goals: cross-replica
session state, distributed rate-limit state, and sticky-session
routing for idempotency replay are out of scope. Cloud profile
assumes single-replica behind ingress.

If an operator deploys multi-replica anyway, expected failure
modes include:

- Idempotency replay misses across replicas (each replica has its
  own LRU cache; a retry routed to a different replica re-executes).
- Rate-limit buckets diverge across replicas (an attacker rotates
  IPs across replicas to multiply the effective rate by N).
- Cancellation by `id_task` only reaches the replica that holds
  the task.

Operators who need multi-replica should front the fork with a
sticky-session ingress AND accept that rate-limit math is per
replica, not aggregate.

### 4.5 Side-channel timing attacks against auth comparison

FastAPI's `HTTPBasic` uses `compare_digest` for credential
comparison. The fork doesn't add a second auth layer with weaker
comparison. Side-channel timing against an authenticated route
(a 401 vs 200 latency difference) is bounded by uvicorn /
Starlette / FastAPI overhead and the rate limit; the fork doesn't
implement constant-time route handlers above that.

### 4.6 Disk integrity (model files, log files)

The fork reads model files, writes log files and gen outputs to
disk. It assumes the filesystem is uncompromised. Tampered
checkpoint files can cause crashes or undefined behaviour;
`tests/cplugapi/test_models_disk.py` covers per-file resilience
(corrupt files yield `error: {code, message}` on their own record,
not a 500 listing-wide), but there is no integrity check
(SHA-256 verification against a manifest) for incoming models.

### 4.7 GPU-side attacks

CUDA driver, GPU firmware, shared sysmem fallback (NVIDIA's
silent VRAM-spill behaviour). Out of scope. The fork's
`gen_timing` log surfaces `peak_vram_mb` so operators can detect
sysmem-fallback symptoms (10–20× slowdown), but the defence is
operator-action, not fork-action.

### 4.8 Supply chain (pip / uv install path)

The fork installs from PyPI via `uv`. Compromised packages would
land in the venv. Out of scope for cplugapi; standard supply-chain
hygiene applies (pin versions, vet new dependencies).

## 5. Authentication posture

### 5.1 Single layer: `--api-auth`

`--api-auth user:pass` is the only credential layer at this prefix.
There is no API-key surface, no JWT validation, no OAuth dance.
This is intentional: the fork's primary deployment is
desktop-loopback where Basic-over-loopback is the right level of
security; the secondary cloud deployment terminates stronger auth
at the ingress and rewrites to `--api-auth` for the fork.

### 5.2 Cloud auth handoff

Operators deploy stronger public auth at the ingress. Pattern:

```
client -> [ingress: validate JWT; on success, rewrite to Basic
           with the fork's service-account credential] -> fork
```

The fork sees Basic, doesn't know about the public auth scheme.
This is the recommended pattern for cloud deployments; documented
in the (forthcoming) W20 cloud runbook.

### 5.3 WebSocket inheritance

WebSocket upgrades under `/cplugapi/v1/*` honour the same Basic
auth. The W2 ws_auth shim parses the Basic header from the
upgrade scope and re-enters the same `auth_dependency` callable
that gates the HTTP private router. Capability:
`security/ws-auth-enforced`.

There are no production WS endpoints today; the shim is
forward-checked so the invariant is enforced when T31
(`/session/stream/{id_task}`) lands.

### 5.4 Intentionally unauthenticated routes

Three routes are public by design:

- `GET /cplugapi/v1/identify` — bootstrap-discovery. The client
  must be able to fingerprint the backend (fork name, version,
  capabilities) before deciding whether to send credentials.
  The capability list is filtered through `_safe_capability` to
  prevent leaks.
- `GET /cplugapi/v1/livez` — k8s liveness probe. Tests the event
  loop only; never auth-gated regardless of `--api-auth`.
- `GET /cplugapi/v1/readyz` — k8s readiness probe. Default body
  is sanitised booleans; `?verbose=1` is auth-gated when
  `--api-auth` is set.

Every other cplugapi route requires Basic credentials when
`--api-auth` is configured.

## 6. Authorization posture

Single-user fork — no per-route or per-resource authorization
model. All authenticated clients have full access to the cplugapi
surface. This matches the `--api-auth` posture: one credential,
all routes.

If a future deployment needs per-route authorization (e.g. a
multi-tenant cloud variant), it would be implemented at the
ingress (a JWT scope claim per route prefix) rather than as a
second layer in the fork. Per §1 non-goals, multi-tenant /
multi-user is out of scope.

## 7. Defence-in-depth catalog

Things layered on top of `--api-auth` to bound damage if the
credential is compromised or one defence fails.

### 7.1 Middleware ordering invariant

The canonical install order
(`ws_auth -> rate_limit -> tracing -> request_id -> shutdown ->
idempotency -> security_middleware -> access_log -> handler`)
is enforced by `tests/cplugapi/test_idempotency.py::test_middleware_install_order_request_id_outside_idempotency`.
A future rebase that reorders the stack — particularly one that
moves `request_id` inside `idempotency` — fails CI. The order is
load-bearing because:

- `ws_auth` outermost: catches WS upgrades before any HTTP-shaped
  layer sees them.
- `rate_limit` next: rejects 429 before paying for any other
  layer's per-request cost.
- `tracing` and `request_id` early: every layer's logs / errors
  carry the IDs.
- `idempotency` before `security_middleware`: replays should not
  bypass security (a cached body that's safe today might not be
  safe tomorrow if the security policy tightens).
- `access_log` innermost: spans every other layer's `dur_ms`.

### 7.2 RFC 9457 envelope code stability

Error codes (`auth_required`, `auth_failed`, `origin_not_allowed`,
`rate_limited`, etc.) are stable strings. They are appended over
time, never renamed. This means a client switching on
`body.code` survives detail-string tweaks; a client switching on
detail-string substrings (the previous norm) breaks silently on
every wording change.

The catalog is in `doc/cplugapi.md` §"Error code catalog".

### 7.3 Capability discovery without auth (W4)

`/identify` exposes `capabilities[]` so a client can negotiate
features before authenticating. The list is sorted (stable for
diff-based monitoring) and filtered through `_safe_capability` so
deployment specifics never reach the public probe. Devlog
2026-05-10-identify-capabilities documents the filter rationale.

### 7.4 Capability deprecation policy (W15)

Capability strings can be renamed via dual emission: `register_with_legacy`
adds both names, with the legacy on the `deprecated_capabilities[]`
egress array. Removal lands in the next minor release after the
Rust client confirms migration. Time-window-elapsed alone does NOT
trigger removal — Rust client confirmation is the load-bearing gate.
Devlog 2026-05-10-capability-namespacing.

### 7.5 Diagnostic logs default off

`CPLUG_ACCESS_LOG`, `CPLUG_SDAPI_OBSERVER`, `CPLUG_GEN_TIMING` all
default to `0` in `webui-user.bat`. The desktop client polls at
~4 Hz during a gen, which would flood the console; operators flip
the streams on while triaging, off otherwise. Modules expose the
"toggle is off" state via the capability registry — disabled
streams are absent from `/health.capabilities[]`, which lets a
client detect "log routing is off, don't expect lines".

Detail strings in the logs are deliberately scoped: request method,
path, status code, duration, content-length, request-id.
**Not** logged: bodies, headers, credentials, internal state.

### 7.6 Cloud profile fail-fast on misconfig

`rate_limit.validate_startup()` raises `RuntimeError` at boot if
cloud profile is active AND any rate-limit class is enabled AND
`CPLUG_TRUSTED_PROXIES` is unset. The error message names the env
var. This catches the most common cloud-deployment misconfig
(forgot to configure the trusted-proxy CIDRs) at deploy time
rather than after first traffic, when XFF spoofing would silently
bypass the rate limit.

## 8. Known gaps / accepted risk

### 8.1 Earlier auth-failed keying bug (fixed)

The first draft of the auth-failed bucket keyed on
`username + password-prefix(4)`. Code review caught that an
attacker varying the password past character 4 gets a fresh
bucket per attempt, defeating the rate limit. **Fixed**: keying is
username-only. Devlog 2026-05-10-rate-limit §"Auth-failure
observability". The `_key` helper in
`modules/cplugapi/rate_limit.py:441-456` carries an in-source note
documenting the regression. Tests:
`test_observe_auth_failures_does_not_throttle_legitimate_user`,
plus the broader auth-wrap suite.

### 8.2 Cloud profile requires `CPLUG_TRUSTED_PROXIES`

`validate_startup()` enforces this. Without trusted proxies
configured, the surface refuses to start in cloud profile when any
rate-limit class is enabled. This is a deliberate fail-fast — the
alternative (start anyway, silently bypass rate limiting) is worse
than refusing to boot.

The pytest bypass (`PYTEST_CURRENT_TEST` env var) exists so test
fixtures that activate cloud profile to exercise other behaviour
don't have to set trusted proxies just to satisfy the gate. Tests
that explicitly cover the validation logic invoke `validate_startup`
directly.

### 8.3 `/sdapi/v1/{txt2img,img2img}` reject during drain (W12)

The `RejectDuringDrainMiddleware` returns 503 on these two
upstream routes when the drain flag is set. This is a deliberate
exception to the byte-identity invariant on `/sdapi/v1/*` (CLAUDE.md
invariant 1). Justification:

- The fork's primary value-add is the live-sketching workflow,
  which depends on gen-call fast-path latency. Letting new gens
  enter during shutdown would lie to the client (those gens get
  interrupted at grace expiry) and waste GPU compute on work
  the client will discard.
- The behaviour is profile-gated (off by default in desktop, on
  by default in cloud) and operator-configurable
  (`CPLUG_SHUTDOWN_REJECT_NEW=0|1`).
- The exception is documented here, not in CLAUDE.md, because
  CLAUDE.md states the invariant unconditionally (the fork
  surface is byte-identical except in this one operationally
  scoped case).

If an operator runs the fork with `CPLUG_SHUTDOWN_REJECT_NEW=0`,
the byte-identity invariant holds even during drain — at the cost
of accepting work that may be killed at grace expiry.

### 8.4 No body-size cap when `Content-Length` is absent

The body-size guard reads `Content-Length` only. Chunked-transfer
without `Content-Length` falls through to whatever cap uvicorn
applies. This is documented in the W7 devlog
(2026-05-10-per-route-body-limits §"Pure-ASGI middleware reading
the message stream"); the desktop client always sends
`Content-Length`, and loopback uvicorn rejects chunked-without-CL
with 411, so the gap is closed in practice. If a threat emerges
where lying clients matter, a stream-counting middleware is the
right next step.

### 8.5 No bound on idempotency cache memory

`CPLUG_IDEMPOTENCY_MAX` defaults to 1024 entries (LRU). The
memory cost depends on response body size; bodies can be up to
the 32 MiB body cap in pathological cases. Worst case: 32 GiB
resident. In practice cplugapi response bodies are <16 KiB, so
the cache is bounded ~16 MiB. Operators with tight memory
budgets can lower `CPLUG_IDEMPOTENCY_MAX`.

### 8.6 No bound on access log throughput

When `CPLUG_ACCESS_LOG=1`, every cplugapi request emits one log
line. A flood of requests floods the log handler. Mitigation:
the fork ships with `CPLUG_ACCESS_LOG=0` by default and the
rate-limit middleware (cloud profile) caps inbound rate. But a
desktop operator who enables access logging during triage and
forgets to disable can flood disk; the operator-facing cost is
documented but not architecturally prevented.

### 8.7 Trace-context spoofing replaces, doesn't reject

A malformed inbound `traceparent` is silently replaced with a
fresh server-generated value. This is correct under W3C semantics
(§3.2.2.5: "vendors SHOULD restart the trace") but means an
attacker can break upstream span correlation by sending malformed
trace context. Out of scope for cplugapi defence — the upstream
proxy's trace pipeline is responsible for noticing the
correlation gap.

### 8.8 Error detail strings inspected by sniff

The RFC 9457 handler sniffs `HTTPException.detail` for substrings
to map to `code` (e.g. detail containing "preset" → `preset_unknown`,
"task" → `task_not_found`). A future endpoint that emits a detail
string containing the wrong substring gets the wrong code. The
escape hatch is `HTTPException(headers={"X-Cplug-Error-Code": "..."})`
which the handler consumes and strips from the response. Devlog
2026-05-10-problem-details-errors §"Per-endpoint exception code".
This is a maintainability gap, not a security gap; clients
switch on `body.code` and the wrong code is wrong but not
dangerous.

### 8.9 Capability registry has no policy for "leak shape"

`_safe_capability` blocks two known leak shapes (hex SHAs,
checkpoint suffixes). A future leak shape (path components that
look like usernames, hostnames, IPs) would not be caught. The
filter is defence-in-depth against accidents; the primary defence
is reviewer caution at capability-registration time. Devlog
2026-05-10-identify-capabilities documents the policy that
public capability names must be deployment-agnostic.

### 8.10 `/version` includes detailed system info

`GET /version` returns Python / platform / framework versions,
torch + CUDA + GPU list. This is auth-gated. The leak vector
materialises only if `--api-auth` is misconfigured (no creds set,
or weak creds). The auth-failed rate limit and credential brute
force defences (§3 row 4) are the upstream mitigations.

### 8.11 No CSRF token / double-submit cookie

The fork uses Origin / Sec-Fetch-Site checks for CSRF defence,
not a token. Token-based CSRF is the wrong shape for a
desktop-companion API: native clients have no UI to embed the
token in, and the loopback model means the token would just round-
trip through the same socket the request came in on. Origin /
Sec-Fetch-Site is the correct browser-adjacent defence; tokens
add complexity without raising the bar.

### 8.12 No content security policy / X-Frame-Options on responses

The fork doesn't set `Content-Security-Policy`, `X-Frame-Options`,
or `Strict-Transport-Security` on cplugapi responses. The desktop
client doesn't render fork-served HTML; the cloud deployment
relies on the ingress to add these for any HTML it serves. The
fork serves only JSON, so the headers add no value at the fork
layer.

## 9. Verification

### 9.1 Per-threat tests

Every entry in §3 has a backing test. The test paths in the
mitigation table point to specific cases.

The aggregate cplugapi suite is `tests/cplugapi/`. Last counted
~546 tests passing, 4 skipped (after W7 + W8 + W9 + W12 + W15
landed). Skipped tests are environment-specific (Windows-only
shutdown signal cases, etc.), not security gaps.

### 9.2 CI gating

`.github/workflows/cplugapi-tests.yml` runs the cplugapi suite on
every push / PR that touches `modules/cplugapi/**`,
`tests/cplugapi/**`, `modules/api/api.py`, or the workflow itself.
A failing security test blocks merge.

### 9.3 Threat-mitigation matrix freshness

This document and the mitigation table in §3 should be updated
whenever:

- A new capability string lands in the registry.
- A new env var lands in `modules/cplugapi/`.
- A new middleware or handler is added.
- An existing mitigation's threat model changes (e.g. the W7
  per-route caps tightening list grows).

The devlog entries (`devlog/YYYY-MM-DD-*.md`) are the change
trail; this document is the rolled-up audit primer.

### 9.4 Pen-test scope

A future pen-test should target the loopback profile (browser
adjacent, DNS rebind, body-size DoS) and the cloud profile (XFF
spoofing, credential brute force, rate-limit bypass, drain-time
abuse). The threats in §4 are out of scope for cplugapi pen-test;
the OS / kernel / supply-chain layers want their own audits.

### 9.5 Open audit items for a future sprint

Gaps the team should prioritise next:

1. **Stream-counting body cap** — close §8.4. Pure-ASGI
   middleware that wraps `receive` and aborts on the first byte
   past the cap. Required if the fork ever sees clients that
   chunk without `Content-Length`.
2. **Memory bound on idempotency cache** — close §8.5. Add a
   per-entry size cap (`CPLUG_IDEMPOTENCY_MAX_ENTRY_BYTES`) so a
   single 32 MiB cached response doesn't dominate the LRU.
3. **Access-log rate cap** — close §8.6. Either a token-bucket on
   log emission or a "log every Nth request when the rate
   exceeds threshold" sampling policy.
4. **Capability-name leak shape policy** — close §8.9. Document
   the policy in `modules/cplugapi/capabilities.py` (probably as
   a `register()`-time validator that rejects non-deployment-
   agnostic strings) so reviewer caution is encoded in code.
5. **Constant-time error paths** — close §8.5 (timing). Audit
   the critical 401 path for branch-time differences between
   "credential malformed", "credential present but wrong", and
   "credential correct". `compare_digest` covers the
   compare-step; the surrounding parsing might not.
6. **Body cap when `Content-Length` is absent** — see (1).
   Same fix.
7. **Manifest integrity for incoming model files** — close
   §4.6. SHA-256 verification against an operator-supplied
   manifest before loading. Out of scope per `plan/cplugapi-world-class.md`,
   but worth revisiting.
8. **`/version` response sanitisation** — §8.10. Either drop
   the detailed system info (and refer triage operators to logs)
   or split into a sanitised public body and a `?verbose=1`
   gated detailed body, mirroring `/readyz`.

These are not blockers for the current release; they are the
list of "things worth doing in the next hardening sprint".
