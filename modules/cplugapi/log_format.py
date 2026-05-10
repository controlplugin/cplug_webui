"""Optional JSON-line formatter for cplugapi-owned loggers.

Cloud operators ship cplugapi logs into ELK / Loki / CloudWatch and
need each line to parse as JSON. The default text format is
grep-friendly key=value (see :mod:`access_log`'s rendered message
shape) — fine for desktop loopback, awkward for log aggregators.

Setting ``CPLUG_LOG_FORMAT=json`` swaps every cplugapi-owned
handler's formatter for :class:`JsonLineFormatter`, which emits one
JSON object per line. The existing call sites already pass an
``extra={...}`` dict on every structured emit; this formatter promotes
those keys to top-level JSON fields verbatim so downstream parsers
see ``request_id``, ``dur_ms``, ``status``, etc. without having to
regex the rendered message.

**Scope is strictly cplugapi.\\*.** Upstream Forge / WebUI loggers
keep their existing formatters — invariant 1 (sdapi byte-identity)
forbids reformatting those streams. The list of cplugapi-owned
loggers is encoded as a tuple constant; new cplugapi loggers must
be appended here when introduced.

**Stdlib-only by design.** ``python-json-logger`` would do this for
us but ships another dep + import-time cost; the formatter below is
~30 LoC and has no failure modes the stdlib doesn't already have.

**Idempotent install.** ``install()`` is safe to call repeatedly —
each call rebinds the same formatter onto the same handlers, which
is a no-op on the second pass.

**Failure modes.** If a structured ``extra`` value is not
JSON-serialisable (e.g. a custom object, a ``Path``, a numpy scalar)
the formatter falls back to ``repr()`` so the line still parses;
this matches the contract of ``json.dumps(default=str)`` but with a
per-key fallback so one bad value doesn't destroy the rest of the
record.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from . import capabilities

# Env var + accepted values. ``CPLUG_LOG_FORMAT=json`` flips JSON
# mode on; anything else (including unset) leaves the existing text
# formatters in place.
ENV_LOG_FORMAT = "CPLUG_LOG_FORMAT"
FORMAT_TEXT = "text"
FORMAT_JSON = "json"

# Loggers owned by cplugapi modules. Reformatting these is in scope;
# anything outside this list is upstream Forge / WebUI territory and
# must be left alone (invariant 1: sdapi byte-identity).
#
# Encoded as a constant so a maintainer adding a new cplugapi logger
# can grep this list instead of guessing what install() touches. The
# list is intentionally explicit rather than ``logging.Logger.manager``-
# scanned for a ``cplugapi.`` prefix, because not every cplugapi.\\*
# logger is part of the structured-emit contract (e.g. capabilities
# warnings, asyncio_filter diagnostics) and silently flipping every
# match would surprise operators.
_CPLUGAPI_LOGGERS = (
    "cplugapi.access",
    "cplugapi.sdapi",
    "cplugapi.gen_timing",
    "cplugapi.upscale",
    "cplugapi.preempt",
    "cplugapi.ws_auth",
)

# Standard attributes a fresh ``logging.LogRecord`` carries — anything
# NOT in this set was added via ``logger.<level>(..., extra={...})``
# and is therefore a structured field we want to surface.
#
# Computed from a synthetic record at import time so the set tracks the
# Python version the runtime actually uses (3.12 added ``taskName``,
# 3.13 may add more). Hand-listing would drift on a Python upgrade.
_STD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord(
        name="_baseline",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="",
        args=None,
        exc_info=None,
    ).__dict__.keys()
) | {"message", "asctime"}  # populated by Formatter, not __init__


class JsonLineFormatter(logging.Formatter):
    """Render every record as a single JSON object on one line.

    Top-level keys:

    - ``ts`` — ISO-8601 UTC, millisecond precision.
    - ``level`` — record level name (``"INFO"``, ``"WARNING"``…).
    - ``logger`` — fully qualified logger name (``"cplugapi.access"``).
    - ``msg`` — the rendered message string (after %-arg interpolation).
    - any ``extra={...}`` keys the caller attached, JSON-serialised
      verbatim. Un-serialisable values fall back to ``repr()`` so the
      line still parses.
    - ``exc_info`` — formatted traceback, only when the record carries one.
    """

    def format(self, record: logging.LogRecord) -> str:
        # gmtime + msecs gives us a stable UTC ISO-8601 string without
        # pulling datetime in for one line. ``time.gmtime(created)``
        # uses the same source clock as the rest of logging so
        # cross-record ordering is preserved.
        gm = time.gmtime(record.created)
        ts = (
            f"{gm.tm_year:04d}-{gm.tm_mon:02d}-{gm.tm_mday:02d}T"
            f"{gm.tm_hour:02d}:{gm.tm_min:02d}:{gm.tm_sec:02d}."
            f"{int(record.msecs):03d}Z"
        )
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Merge non-standard LogRecord attrs (i.e. anything passed via
        # ``extra={...}`` at call time). Per-key fallback to repr() so
        # one un-serialisable value doesn't sink the whole line.
        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key in payload:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        # default=str keeps us safe against any value that slipped past
        # the per-key check (e.g. a dict whose VALUE is un-jsonable);
        # it won't override the explicit repr() above because that key
        # is already a string.
        return json.dumps(payload, default=str, ensure_ascii=False)


def is_json_mode() -> bool:
    """True iff ``CPLUG_LOG_FORMAT=json`` (case/whitespace tolerant)."""
    raw = os.environ.get(ENV_LOG_FORMAT, FORMAT_TEXT)
    return raw.strip().lower() == FORMAT_JSON


def install() -> None:
    """Replace the formatter on every cplugapi-owned handler.

    No-op when not in JSON mode. Idempotent — repeated calls re-bind
    the same formatter.

    The router calls this once at boot, after each cplugapi module
    has constructed its logger (Forge's ``setup_logger`` attaches the
    Rich console handler; we replace its formatter, not the handler,
    so console capture / level routing is preserved).
    """
    if not is_json_mode():
        return
    formatter = JsonLineFormatter()
    for name in _CPLUGAPI_LOGGERS:
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler.setFormatter(formatter)


def register_capabilities() -> None:
    """Advertise JSON-line logging only when actually active.

    Capability is conditional rather than always-on so a desktop
    operator running the default text format doesn't get a misleading
    ``observability/log-format-json`` in ``/health.capabilities``.
    """
    if is_json_mode():
        capabilities.register("observability/log-format-json")
