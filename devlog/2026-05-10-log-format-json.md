# 2026-05-10 — JSON-line log format for cplugapi loggers (W9)

**Kind**: observability — opt-in formatter swap.
**Files**: `modules/cplugapi/log_format.py` (new),
`tests/cplugapi/test_log_format.py` (new). Router wire-up
(`install()` + `register_capabilities()` calls) is a follow-up edit to
`router.py` performed by the maintainer integrating this change.
**Capability**: `observability/log-format-json` (only when
`CPLUG_LOG_FORMAT=json`).
**Rollback**: unset `CPLUG_LOG_FORMAT`. The default is text mode and
`install()` is a no-op there, so the rollback path is "do nothing".
For a hard rollback, drop the call to `log_format.install()` from
`router.setup_cplugapi`.

## Symptom

Cloud operators shipping cplugapi logs into ELK / Loki / CloudWatch /
Grafana need each line to parse as a JSON object so they can index
fields and alert on them. Today every cplugapi-owned logger
(`cplugapi.access`, `cplugapi.sdapi`, `cplugapi.gen_timing`,
`cplugapi.upscale`, `cplugapi.preempt`, `cplugapi.ws_auth`) emits
grep-friendly key=value text — fine for `tail -f` on a developer
laptop, awkward for log aggregators because the fields have to be
re-parsed on every ingest with a fragile regex. The structured
fields are already attached to each record via `extra={...}` (see
`access_log._emit`) but never rendered as JSON.

## Root cause

No JSON formatter is installed on the cplugapi loggers. Forge's
`backend.logging.setup_logger` attaches a Rich console handler with a
plain `%(name)s :: %(levelname)s :: %(message)s` formatter. The
structured `extra` dict is therefore present on the `LogRecord`
object in memory but invisible in the rendered output unless the
caller pre-formatted it into the message (which `access_log` does,
producing the key=value text).

## Alternatives considered

### Option A — depend on `python-json-logger`

Mature, drop-in `JsonFormatter` class. ~1k LoC.

**Rejected** because (a) it adds a hard dep where 30 LoC of stdlib
gets us identical behaviour for our six loggers, (b) the cplugapi
project deliberately keeps its dep surface small (sibling spec audit
01 §4 lists transitive dep weight as a fork-deviation cost), and
(c) the package's failure modes (un-jsonable values raise
`TypeError`) would still require our own per-key fallback.

### Option B — adopt `structlog`

Production-grade, structured-from-day-one. Forces every log call to
go through structlog's API.

**Rejected** because we'd have to rewrite every existing emit site
(`access_log`, `sdapi_observer`, `gen_timing`, `upscale_log`,
`auto_preempt`, `ws_auth`) to use structlog's BoundLogger interface.
That's 6 modules and ~100 emit sites for a feature that is opt-in
and operator-facing; the integration cost dwarfs the value. Existing
`logger.info(msg, extra={...})` is already structured under the hood
— we just need a different render strategy.

### Option C — add a JSON sub-handler instead of swapping formatters

Attach a second handler that renders JSON, leaving the existing
text-rendering handler in place so the operator gets both streams.

**Rejected** because JSON-mode operators don't want a duplicated text
stream cluttering their stdout — they pipe stdout into their log
shipper expecting one record per line. Two handlers would either
double-emit (parser rejects half) or require routing config that the
operator now has to maintain. The opt-in formatter swap keeps the
default behaviour bit-identical and the JSON behaviour clean.

### Option D — swap formatters on every `cplugapi.*` logger via prefix scan

Walk `logging.Logger.manager.loggerDict` looking for names starting
with `cplugapi.`.

**Rejected** because cplugapi has loggers (`cplugapi.asyncio_filter`,
`cplugapi.capabilities`-style internal warnings) that aren't part of
the structured-emit contract. Auto-flipping every match would
silently surprise an operator who expected one of those to keep its
console-friendly format. CLAUDE.md's monkey-patch guidance applies
by analogy: explicit allow-lists survive rebases, prefix scans don't
surface coverage gaps.

## Decision

New module `modules/cplugapi/log_format.py` exporting:

- `JsonLineFormatter(logging.Formatter)` — renders each record as one
  JSON object with `ts` (ISO-8601 UTC ms), `level`, `logger`, `msg`,
  every `extra={...}` key the caller attached, and `exc_info` when
  the record carries an exception.
- `is_json_mode()` — reads `CPLUG_LOG_FORMAT` (case/whitespace
  tolerant), returns True iff value is `json`.
- `install()` — no-op in text mode; in JSON mode replaces the
  formatter on every handler attached to the six cplugapi-owned
  loggers, listed explicitly in `_CPLUGAPI_LOGGERS`.
- `register_capabilities()` — registers
  `observability/log-format-json` only when JSON mode is active.

The standard-record-attribute set used to filter out framework
metadata (`filename`, `lineno`, `thread`, etc.) is computed from a
synthetic `LogRecord` at import time, so it tracks whatever Python
version the runtime uses (3.12 added `taskName`; 3.13 may add more).
Hand-listing would drift on a Python upgrade.

Per-key `json.dumps(value)` probe with `repr()` fallback means an
un-serialisable `extra` value (a `Path`, a custom object, a numpy
scalar) does not raise — the key is preserved as its repr form so
the line still parses as JSON. `json.dumps(payload, default=str)`
provides a second safety net for nested values.

## Blast radius

**Default (`CPLUG_LOG_FORMAT` unset / `text`).** Zero functional
change. `install()` returns immediately; `register_capabilities()`
does not register anything. No formatter swap, no log-line
modification, no capability advertised. Existing tests continue to
exercise the text format unchanged.

**JSON mode (`CPLUG_LOG_FORMAT=json`).** The six cplugapi loggers
emit one JSON object per line instead of key=value text. Operators
who scrape `cplugapi.access` log lines for `req_id=req_…` will need
to switch to JSON parsing (`payload["request_id"]`). The structured
fields (`request_id`, `dur_ms`, `status`, `method`, `path`,
`in_bytes`, `out_bytes`, `replayed`, `error`) are unchanged in name
and shape — only the rendering differs.

**Upstream loggers untouched.** `install()` only iterates the
explicit `_CPLUGAPI_LOGGERS` tuple; no `cplugapi.asyncio_filter`,
no `modules.shared`, no Forge boot-time logger gets reformatted.
Invariant 1 (sdapi byte-identity) holds because sdapi log streams
go through their own loggers which are not in the allow-list.

## Failure modes

1. **Operator runs both modes via stdout redirection.** The env var
   is read once per `is_json_mode()` call — `install()` is called
   from router boot, so flipping the var post-boot has no effect
   until restart. Documented behaviour; matches `access_log`'s
   read-once kill switch.
2. **`extra={...}` value is un-jsonable.** Per-key `json.dumps`
   probe falls back to `repr(value)`. The key still appears in the
   output, with a string value. No exception escapes the formatter,
   no log line is dropped.
3. **Custom logger attached at runtime by an extension.** Extension
   loggers using one of the six cplugapi-owned names will get their
   handlers reformatted on the next `install()` call. Extensions
   using their own logger name (the convention) are unaffected.
4. **Logger has no handlers at install time.** `install()` iterates
   `logger.handlers` directly; an empty list is a benign no-op. If
   handlers are attached AFTER `install()`, those handlers will
   render with the default text formatter — operators who attach
   handlers post-boot must call `install()` again.
5. **Rich handler in JSON mode.** Forge's `setup_logger` attaches a
   Rich console handler. Replacing its formatter with
   `JsonLineFormatter` keeps Rich's level routing / colourisation
   off the rendered line (we control the format string), but the
   handler still routes to the same destination. Operators see one
   JSON object per line on stdout; Rich's colour codes are not
   embedded.

## Test surface

`tests/cplugapi/test_log_format.py` covers:

- Env-var parsing (default, explicit text, json, case/whitespace
  tolerance, garbage rejection).
- `install()` no-op in text mode (preserves original formatter).
- `install()` swap in JSON mode for every name in
  `_CPLUGAPI_LOGGERS`.
- `install()` idempotent.
- Round-trip: text mode produces non-JSON output; JSON mode produces
  parseable JSON with caller-supplied `extra` keys preserved.
- Access-log emission shape (`request_id`, `method`, `path`,
  `status`, `dur_ms`, `replayed`) survives the formatter.
- Un-jsonable `extra` value falls back to repr without raising.
- `exc_info` records render a formatted traceback under
  `payload["exc_info"]`.
- Standard `LogRecord` attrs (`filename`, `lineno`, `thread`, …) do
  NOT leak into the JSON payload.
- `register_capabilities()` skipped in text mode, active in JSON
  mode.
- Upstream-named loggers (`modules.shared`) are NOT reformatted.
- Locked top-level keys (`ts`, `level`, `logger`, `msg`) always
  present.

19 new tests; full cplugapi suite passes (435 passed, 4 skipped).
