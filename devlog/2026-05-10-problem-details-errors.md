# 2026-05-10 — RFC 9457 Problem Details error envelope (W3)

**Kind**: new module + cross-cutting envelope migration.
**Files**: `modules/cplugapi/errors.py` (new),
`modules/cplugapi/security_middleware.py`,
`modules/cplugapi/idempotency.py`, `modules/cplugapi/router.py`,
`tests/cplugapi/test_errors.py` (new),
`tests/cplugapi/test_idempotency.py` (one assertion updated).
**Capability**: `error-format-problem-details` (RFC-version-agnostic).
**Rollback**: revert `errors.install_handlers(app)` call in
`router.py:setup_cplugapi`, restore the inline `_reject` body in
`security_middleware.py` and the inline `JSONResponse({"error", "detail"})`
in `idempotency.py`. Capability comes off automatically.

## Symptom

The cplugapi surface emitted three distinct error response shapes:

- `security_middleware._reject` returned `{"detail": "..."}` with the
  default `application/json` media type.
- `idempotency.dispatch` (invalid-key 400) returned
  `{"error": "invalid_idempotency_key", "detail": "..."}`.
- FastAPI's default `HTTPException` handler returned `{"detail": "..."}`
  for any HTTPException raised inside a handler.

A client decoder had to handle three shapes. Worse, none of them
carried a stable machine-switchable `code` — clients switched on
HTTP status + magic substrings of the `detail` text, which broke
silently on every detail-string tweak.

## Root cause

The cplugapi modules were written incrementally over Phases 1–B.
Each module owned its own error shape — internally consistent,
externally divergent. There was no errors module.

## Decision

Adopt RFC 9457 (Problem Details for HTTP APIs, July 2023; obsoletes
RFC 7807 with full wire compatibility). The capability string is
deliberately RFC-version-agnostic — `error-format-problem-details` —
so the client codegen survives any future RFC revision without
churn.

New module `modules/cplugapi/errors.py` exports:

- `cplugapi_problem(*, status, code, detail, ...) -> JSONResponse` —
  the canonical builder. All cplugapi error responses now go through
  it.
- `CODES` — class holding stable snake_case error-code constants.
  Initial set covers idempotency, security, and the common
  endpoint-level errors (auth, validation, body too large, etc.).
- `cplugapi_http_exception_handler` and
  `cplugapi_validation_exception_handler` — global FastAPI exception
  handlers that defer to FastAPI's defaults for any path outside
  `/cplugapi/v1/`. This means `HTTPException` raised inside cplugapi
  endpoints (e.g. a 401 from the auth dep, a 404 from `forge/preset`)
  surfaces as problem+json automatically; an HTTPException raised in
  `/sdapi/v1/*` keeps FastAPI's default `{detail: ...}` body —
  invariant 1 (byte-identity) preserved.
- `install_handlers(app)` wires both above onto the FastAPI app.

The envelope:

```json
{
  "type": "about:blank",
  "title": "Forbidden",
  "status": 403,
  "detail": "origin not allowed: http://evil.example",
  "code": "origin_not_allowed",
  "request_id": "req_..."
}
```

Validation errors (RFC 9457 multiple-problems extension):

```json
{
  "type": "about:blank",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "request validation failed",
  "code": "validation_failed",
  "request_id": "req_...",
  "errors": [
    {"loc": ["body", "n"], "msg": "Input should be a valid integer", "type": "int_parsing"},
    ...
  ]
}
```

## Alternatives considered

### Plain `{error, code, detail}`

Simpler but no media-type discriminator (`application/problem+json`
lets clients route problem responses through one decoder regardless
of endpoint), no aggregation extension, no RFC alignment.

**Rejected** — industry consolidation around 9457 makes this a
short-term win; we'd be inventing a non-standard error contract for
cosmetic simplicity.

### Content negotiation

`Accept: application/problem+json` -> 9457; default -> legacy
`{detail}`. Strictly safer for legacy clients.

**Rejected** — cplugapi clients are first-party (the Rust
ControlPlugin desktop binary). We can update the codegen at the same
release; doubling the surface to support an Accept header isn't
worth the complexity.

### Ship 9457 only, drop top-level `detail`

Cleanest. Breaks any current client decoder that reads `body.detail`
top-level.

**Decision**: keep `detail` populated alongside the structured
envelope through one minor release per the §3.1 deprecation policy
in `plan/cplugapi-world-class.md`. After one minor of dual emission,
the legacy top-level `detail` is removed. The `error` key (only used
by idempotency's invalid-key 400) is dropped immediately — there's
no point keeping a different field name through deprecation; the
test assertion was updated in the same change.

### Per-endpoint exception code via `HTTPException` arg

FastAPI's `HTTPException` doesn't natively carry a custom code field.
Workarounds: subclass HTTPException; pass code via `headers={"X-Cplug-Error-Code": "..."}`;
sniff `detail` text in the handler.

**Decision**: support both. The handler sniffs `detail` text on
common cases (`detail` contains "preset" → `preset_unknown`;
"task" → `task_not_found`; otherwise the default for that status).
For endpoints that need to override, raise
`HTTPException(headers={"X-Cplug-Error-Code": "..."})` — the header
is consumed by the handler and stripped from the response. ~5 LoC
extra, zero new exception subclasses.

## Blast radius

- **Wire shape change**: every cplugapi error response now has
  `application/problem+json` media type and `code`/`request_id`
  fields. Clients that read `body.detail` keep working through the
  one-minor dual-emission window.
- **`/sdapi/v1/*`**: zero change. Exception handlers defer to
  FastAPI's defaults for non-cplugapi paths; verified by
  `test_httpexception_outside_cplugapi_uses_default_handler` and
  `test_validation_error_outside_cplugapi_uses_default`.
- **`request_id` correlation**: the security middleware sits
  OUTSIDE the request_id middleware in the canonical install order
  (`plan/cplugapi-world-class.md` §3.0), so `request.state.request_id`
  isn't stamped when security rejects. The helper falls back to the
  inbound `X-Request-Id` header so client-supplied ids still
  surface; an unsourced rejection just omits the field rather than
  emitting a placeholder.

## Failure modes

1. **Upstream uncomments `api_middleware(self.app)` later** —
   that registers a global `HTTPException` handler. Whichever runs
   last wins. cplugapi's handler defers to the FastAPI default for
   non-cplugapi paths, but the *upstream* handler is custom
   (`handle_exception`). Mitigation: when this rebase issue arises,
   update `cplugapi_http_exception_handler` to defer to the upstream
   handler directly (one-line change). For now the upstream handler
   is commented out in `modules/api/api.py:210`.
2. **A new endpoint emits `HTTPException(detail=...)` with a status
   we don't know how to code** — `_http_code_for` falls back to
   `"http_error"`. Endpoints that need a specific code use the
   `X-Cplug-Error-Code` header escape hatch.
3. **Pydantic validation error includes an un-jsonable `ctx` value**
   (Pydantic v2 sometimes attaches the wrapped exception object) —
   `_serialize_pydantic_error` repr's the offender so the response
   stays jsonable. Tested.

## Test surface

`tests/cplugapi/test_errors.py` (14 cases) covers:

- Helper unit tests (minimal envelope, optional fields, default titles).
- Security middleware emits problem+json on origin/host/body rejection.
- Idempotency middleware emits problem+json on invalid key.
- HTTPException → problem+json on cplugapi paths.
- HTTPException → default `{detail}` on `/sdapi/v1/*` paths.
- RequestValidationError → problem+json with `errors[]` extension on
  cplugapi paths.
- RequestValidationError → default body on `/sdapi/v1/*` paths.
- `X-Cplug-Error-Code` header overrides the sniff and is stripped
  from the response.
- `request_id` field populated when client sends `X-Request-Id`.
- Capability registration.

Existing `test_idempotency.py::test_malformed_key_rejected_with_400`
updated: assertion now reads `code == "idempotency_key_invalid"`
instead of the old `error == "invalid_idempotency_key"`.

Full cplugapi suite passes (368 tests, 4 skipped).
