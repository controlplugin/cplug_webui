"""RFC 9457 Problem Details error envelope for ``/cplugapi/v1/*``.

RFC 9457 (July 2023) obsoletes RFC 7807 with full wire compatibility:
same ``application/problem+json`` media type, same standard fields.
We adopt 9457 for two reasons:

1. The 'multiple problems' extension lets ``RequestValidationError``
   emit a list of field errors as an ``errors[]`` array on the problem
   document instead of collapsing the list into one ``detail`` string.
2. Capability string ``error-format-problem-details`` is RFC-version-
   agnostic — the client codegen never has to track which RFC we
   reference.

The envelope adds two non-RFC fields beyond the standard set:

- ``code: str`` — stable, machine-switchable error identifier (a value
  from :class:`CODES`). Clients switch on this; humans read ``detail``.
- ``request_id: str`` — same value as the ``X-Request-Id`` response
  header, for log correlation.

**Backwards compat (one-minor-release deprecation window):** every
problem-details response also populates the top-level ``detail`` key
alongside the structured envelope, so existing clients that read
``body.detail`` directly still work. After one minor release of
dual-emission, the legacy top-level ``detail`` is removed per the
§3.1 deprecation policy in ``plan/cplugapi-world-class.md``.

Scoping: the global FastAPI exception handlers installed by
:func:`install_handlers` defer to FastAPI's defaults for any path
outside ``/cplugapi/v1/`` so ``/sdapi/v1/*`` byte-identity (invariant
1 in ``CLAUDE.md``) is preserved. cplugapi-internal middleware (e.g.
:mod:`security_middleware`, :mod:`idempotency`) calls
:func:`cplugapi_problem` directly without going through the handler
chain.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler as _fastapi_http_handler,
)
from fastapi.exception_handlers import (
    request_validation_exception_handler as _fastapi_validation_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import capabilities

# Wire identifier — RFC 9457 keeps the RFC 7807 media type unchanged.
PROBLEM_JSON = "application/problem+json"

_PREFIX = "/cplugapi/v1/"


class CODES:
    """Stable, machine-switchable error code constants.

    Codes are snake_case strings. New codes are appended; existing
    codes are NEVER renamed (clients switch on the literal). The
    catalog in ``doc/cplugapi.md`` enumerates each code with its
    HTTP status and meaning.
    """

    # idempotency middleware
    IDEMPOTENCY_KEY_INVALID = "idempotency_key_invalid"
    IDEMPOTENCY_KEY_TOO_LONG = "idempotency_key_too_long"  # reserved
    # security middleware
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    HOST_NOT_ALLOWED = "host_not_allowed"
    SEC_FETCH_SITE_NOT_ALLOWED = "sec_fetch_site_not_allowed"
    BODY_TOO_LARGE = "body_too_large"
    INVALID_CONTENT_LENGTH = "invalid_content_length"
    # endpoint handlers
    TASK_NOT_FOUND = "task_not_found"
    PRESET_UNKNOWN = "preset_unknown"
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    # FastAPI/Pydantic
    VALIDATION_FAILED = "validation_failed"
    # rate limiting (W8)
    RATE_LIMITED = "rate_limited"
    # generic fallback
    HTTP_ERROR = "http_error"


_DEFAULT_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def cplugapi_problem(
    *,
    status: int,
    code: str,
    detail: str,
    title: Optional[str] = None,
    type_uri: str = "about:blank",
    instance: Optional[str] = None,
    request_id: Optional[str] = None,
    errors: Optional[list[dict[str, Any]]] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    """Build a ``application/problem+json`` response.

    The standard RFC 9457 fields are populated unconditionally
    (``type``, ``title``, ``status``, ``detail``); ``instance`` is
    optional. The non-RFC additions are ``code`` (always set —
    callers must pass a CODES value) and ``request_id`` (set when the
    caller has it, omitted otherwise).

    ``errors`` is the RFC 9457 multiple-problems extension — a list
    of sub-problems used by the validation handler to surface every
    Pydantic field error in one response instead of collapsing.
    """
    body: dict[str, Any] = {
        "type": type_uri,
        "title": title or _DEFAULT_TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "code": code,
    }
    if instance is not None:
        body["instance"] = instance
    if request_id is not None:
        body["request_id"] = request_id
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Exception handlers — global registration, scoped by path
# ---------------------------------------------------------------------------


def _http_code_for(status: int, detail: Any) -> str:
    """Pick a CODES value for an ``HTTPException`` raised somewhere we
    don't control directly (FastAPI auth dep, custom 401 in handler).

    Sniffs the detail string for keyword matches on 404 since multiple
    endpoints raise 404 with distinct meanings (preset vs task).
    Routes that raise their own ``HTTPException`` can attach a
    ``headers={"X-Cplug-Error-Code": <code>}`` to override the sniff."""
    if status == 401:
        return CODES.AUTH_REQUIRED
    if status == 403:
        return CODES.AUTH_FAILED
    if status == 413:
        return CODES.BODY_TOO_LARGE
    if status == 422:
        return CODES.VALIDATION_FAILED
    if status == 429:
        return CODES.RATE_LIMITED
    if status == 404 and isinstance(detail, str):
        d = detail.lower()
        if "preset" in d:
            return CODES.PRESET_UNKNOWN
        if "task" in d:
            return CODES.TASK_NOT_FOUND
    return CODES.HTTP_ERROR


async def cplugapi_http_exception_handler(request: Request, exc: HTTPException):
    """Convert ``HTTPException`` to problem+json for cplugapi paths.

    Outside the cplugapi prefix, defers to FastAPI's default handler so
    ``/sdapi/v1/*`` byte-identity is preserved (invariant 1).

    Handler-supplied error code: when an endpoint raises
    ``HTTPException(headers={"X-Cplug-Error-Code": "preset_unknown"})``,
    the header value overrides the keyword sniff. Other headers on the
    exception are passed through verbatim.
    """
    if not request.url.path.startswith(_PREFIX):
        return await _fastapi_http_handler(request, exc)

    headers = dict(exc.headers) if exc.headers else None
    code = None
    if headers and "X-Cplug-Error-Code" in headers:
        code = headers.pop("X-Cplug-Error-Code")
    if not code:
        code = _http_code_for(exc.status_code, exc.detail)

    detail_str = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    rid = getattr(request.state, "request_id", None)
    return cplugapi_problem(
        status=exc.status_code,
        code=code,
        detail=detail_str,
        request_id=rid,
        headers=headers if headers else None,
    )


async def cplugapi_validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    """Pydantic validation -> problem+json with the ``errors[]``
    extension. Outside the cplugapi prefix, defers to FastAPI's default."""
    if not request.url.path.startswith(_PREFIX):
        return await _fastapi_validation_handler(request, exc)

    rid = getattr(request.state, "request_id", None)
    return cplugapi_problem(
        status=422,
        code=CODES.VALIDATION_FAILED,
        detail="request validation failed",
        request_id=rid,
        errors=[_serialize_pydantic_error(e) for e in exc.errors()],
    )


def _serialize_pydantic_error(err: dict[str, Any]) -> dict[str, Any]:
    """Drop un-jsonable values that Pydantic v2 sometimes attaches in
    ``ctx`` (e.g. a wrapped exception instance). Replace with ``repr``
    so the error stays informative without breaking the JSON encoder."""
    safe: dict[str, Any] = {}
    for k, v in err.items():
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            safe[k] = repr(v)
        else:
            safe[k] = v
    return safe


# ---------------------------------------------------------------------------
# Wire-up
# ---------------------------------------------------------------------------


def install_handlers(app: FastAPI) -> None:
    """Register the exception handlers on ``app``.

    FastAPI's exception-handler registry is keyed by exception class.
    Re-registration replaces the prior handler — idempotent for our
    purposes since both calls install the same callable. The handlers
    we install defer to FastAPI's defaults for non-cplugapi paths so
    upstream behaviour is preserved (invariant 1).
    """
    app.add_exception_handler(HTTPException, cplugapi_http_exception_handler)
    app.add_exception_handler(
        RequestValidationError, cplugapi_validation_exception_handler
    )


def register_capabilities() -> None:
    """Advertise the structured error format. RFC-version-agnostic
    string so the capability survives the eventual 9457→successor
    transition unchanged."""
    capabilities.register("error-format-problem-details")
