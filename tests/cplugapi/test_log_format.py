"""Tests for ``modules.cplugapi.log_format``.

The JSON-line formatter must:

- Be a no-op when ``CPLUG_LOG_FORMAT`` is unset / ``text``.
- Replace formatters on cplugapi-owned handlers when
  ``CPLUG_LOG_FORMAT=json``.
- Emit one parseable JSON object per record with ``ts`` / ``level`` /
  ``logger`` / ``msg`` plus every key the caller attached via
  ``extra={...}``.
- Survive un-jsonable ``extra`` values (fall back to repr, do not
  raise).
- Only register the capability when JSON mode is active.
- Touch only cplugapi-owned loggers; upstream loggers stay on their
  default formatters (invariant 1).
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from modules.cplugapi import log_format


@pytest.fixture(autouse=True)
def _restore_logger_formatters():
    """Snapshot + restore every cplugapi-owned logger's handler formatters.

    ``install()`` mutates real, process-wide loggers — replacing the
    formatter on whatever handler Forge / earlier tests attached. If a
    test calls ``install()`` in JSON mode and we don't restore, every
    subsequent test in the same pytest run sees JSON-formatted log
    output on cplugapi loggers, which breaks tests that assert the
    presence of plain-text key=value content (e.g. ws_auth, upscale_log).

    The fixture takes a per-handler formatter snapshot up front and
    re-binds the original formatter in teardown. ``autouse=True`` so it
    covers every test in this file without per-test boilerplate.
    """
    snapshots: list[tuple[logging.Handler, logging.Formatter | None]] = []
    for name in log_format._CPLUGAPI_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            snapshots.append((handler, handler.formatter))
    try:
        yield
    finally:
        for handler, original in snapshots:
            handler.setFormatter(original)


@pytest.fixture
def reset_env(monkeypatch):
    """Ensure CPLUG_LOG_FORMAT is unset for the test by default.

    Tests that need json mode set it explicitly via monkeypatch.
    """
    monkeypatch.delenv(log_format.ENV_LOG_FORMAT, raising=False)
    yield monkeypatch


def _attach_capture_handler(logger_name: str) -> tuple[logging.Logger, io.StringIO, logging.StreamHandler]:
    """Attach a fresh StreamHandler with a default text formatter.

    Returns (logger, captured stream, handler). Caller is responsible
    for removing the handler in teardown so repeat tests don't stack.
    """
    logger = logging.getLogger(logger_name)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    # Default text formatter — what install() should REPLACE in JSON
    # mode. In text mode it should be left untouched.
    handler.setFormatter(logging.Formatter("%(name)s :: %(levelname)s :: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger, stream, handler


def test_is_json_mode_default_false(reset_env):
    """Unset env → text mode."""
    assert log_format.is_json_mode() is False


def test_is_json_mode_text_explicit(reset_env):
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "text")
    assert log_format.is_json_mode() is False


def test_is_json_mode_json(reset_env):
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    assert log_format.is_json_mode() is True


def test_is_json_mode_case_and_whitespace(reset_env):
    """Tolerate ``  JSON  `` / ``Json`` / etc."""
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "  JSON  ")
    assert log_format.is_json_mode() is True
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "Json")
    assert log_format.is_json_mode() is True


def test_is_json_mode_garbage_is_text(reset_env):
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "yaml")
    assert log_format.is_json_mode() is False


def test_install_no_op_in_text_mode(reset_env):
    """Without the env var set, install() must not touch handler formatters."""
    logger, _stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        original_formatter = handler.formatter
        log_format.install()
        assert handler.formatter is original_formatter, (
            "install() in text mode must leave formatters as-is"
        )
    finally:
        logger.removeHandler(handler)


def test_install_swaps_formatter_in_json_mode(reset_env):
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    logger, _stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        assert isinstance(handler.formatter, log_format.JsonLineFormatter)
    finally:
        logger.removeHandler(handler)


def test_install_is_idempotent(reset_env):
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    logger, _stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        first = handler.formatter
        log_format.install()
        # Second install also installs a JsonLineFormatter — the type
        # is what matters, not object identity (a fresh formatter is
        # constructed each call, but that's a no-op in practice).
        assert isinstance(handler.formatter, log_format.JsonLineFormatter)
        assert isinstance(first, log_format.JsonLineFormatter)
    finally:
        logger.removeHandler(handler)


def test_install_covers_every_documented_logger(reset_env):
    """All names in _CPLUGAPI_LOGGERS get formatters swapped."""
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    handlers: list[tuple[logging.Logger, logging.Handler]] = []
    try:
        for name in log_format._CPLUGAPI_LOGGERS:
            logger, _stream, handler = _attach_capture_handler(name)
            handlers.append((logger, handler))
        log_format.install()
        for _logger, handler in handlers:
            assert isinstance(handler.formatter, log_format.JsonLineFormatter)
    finally:
        for logger, handler in handlers:
            logger.removeHandler(handler)


def test_text_mode_emits_text(reset_env):
    """Round-trip: text mode → message is plain text, not JSON."""
    logger, stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        # No install() in text mode — formatter remains the text default.
        logger.info("test", extra={"foo": 1})
        line = stream.getvalue().strip()
        assert "cplugapi.access" in line
        assert "INFO" in line
        # Must NOT parse as JSON.
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)
    finally:
        logger.removeHandler(handler)


def test_json_mode_emits_parseable_json(reset_env):
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    logger, stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        logger.info("hello world", extra={"foo": 1, "bar": "baz"})
        line = stream.getvalue().strip()
        payload = json.loads(line)
        assert payload["msg"] == "hello world"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "cplugapi.access"
        assert payload["foo"] == 1
        assert payload["bar"] == "baz"
        # Timestamp must look ISO-8601-ish.
        assert payload["ts"].endswith("Z")
        assert "T" in payload["ts"]
    finally:
        logger.removeHandler(handler)


def test_json_mode_extra_with_request_id_shape(reset_env):
    """Mirror the access_log emission shape — request_id, dur_ms, etc."""
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    logger, stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        logger.info(
            "GET /cplugapi/v1/health status=200",
            extra={
                "request_id": "req_abc",
                "method": "GET",
                "path": "/cplugapi/v1/health",
                "status": 200,
                "in_bytes": -1,
                "out_bytes": 42,
                "replayed": False,
                "dur_ms": 1.234,
            },
        )
        payload = json.loads(stream.getvalue().strip())
        assert payload["request_id"] == "req_abc"
        assert payload["method"] == "GET"
        assert payload["status"] == 200
        assert payload["dur_ms"] == 1.234
        assert payload["replayed"] is False
    finally:
        logger.removeHandler(handler)


def test_json_mode_unjsonable_extra_falls_back_to_repr(reset_env):
    """An object with no JSON encoding must NOT raise; it gets repr'd."""
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")

    class Custom:
        def __repr__(self) -> str:
            return "<Custom obj>"

    logger, stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        logger.info("test", extra={"obj": Custom(), "ok": 1})
        line = stream.getvalue().strip()
        payload = json.loads(line)  # must parse — no exception
        assert payload["obj"] == "<Custom obj>"
        assert payload["ok"] == 1
    finally:
        logger.removeHandler(handler)


def test_json_mode_handles_exception_info(reset_env):
    """Records with exc_info include a formatted traceback."""
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    logger, stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("got an error", extra={"where": "test"})
        payload = json.loads(stream.getvalue().strip())
        assert payload["msg"] == "got an error"
        assert payload["where"] == "test"
        assert "ValueError" in payload["exc_info"]
        assert "boom" in payload["exc_info"]
    finally:
        logger.removeHandler(handler)


def test_json_mode_excludes_standard_record_attrs(reset_env):
    """Standard LogRecord attrs (filename, lineno, etc.) must NOT leak.

    Otherwise every JSON line gets bloated with framework metadata
    that's already implicit in ``logger`` + ``ts``.
    """
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    logger, stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        logger.info("test", extra={"foo": 1})
        payload = json.loads(stream.getvalue().strip())
        for attr in ("filename", "lineno", "funcName", "module", "thread",
                     "processName", "process", "msecs", "relativeCreated",
                     "args", "pathname"):
            assert attr not in payload, f"unexpected std attr {attr!r} in payload"
    finally:
        logger.removeHandler(handler)


def test_register_capabilities_skipped_in_text_mode(reset_env, clean_capabilities):
    """No capability when env unset / text mode."""
    log_format.register_capabilities()
    assert "observability/log-format-json" not in clean_capabilities.enabled_capabilities()


def test_register_capabilities_active_in_json_mode(reset_env, clean_capabilities):
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    log_format.register_capabilities()
    assert "observability/log-format-json" in clean_capabilities.enabled_capabilities()


def test_install_does_not_touch_upstream_loggers(reset_env):
    """An upstream-named logger keeps its original formatter even in JSON mode.

    Invariant 1: cplugapi must not reformat upstream log streams. Only
    loggers in _CPLUGAPI_LOGGERS get the JSON formatter installed.
    """
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    upstream, _stream, handler = _attach_capture_handler("modules.shared")
    original_formatter = handler.formatter
    try:
        log_format.install()
        assert handler.formatter is original_formatter, (
            "install() must NOT reformat upstream loggers"
        )
    finally:
        upstream.removeHandler(handler)


def test_json_payload_keys_have_documented_shape(reset_env):
    """Top-level keys are ``ts``, ``level``, ``logger``, ``msg`` plus extras.

    Lock the contract — downstream JSON parsers (Loki, Elastic) will
    grow alerting on these names.
    """
    reset_env.setenv(log_format.ENV_LOG_FORMAT, "json")
    logger, stream, handler = _attach_capture_handler("cplugapi.access")
    try:
        log_format.install()
        logger.info("hi")
        payload = json.loads(stream.getvalue().strip())
        for key in ("ts", "level", "logger", "msg"):
            assert key in payload, f"missing locked top-level key {key!r}"
    finally:
        logger.removeHandler(handler)
