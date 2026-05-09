"""Tests for ``modules.cplugapi.asyncio_filter``.

The filter targets a specific Windows asyncio cleanup race:
``ConnectionResetError`` raised inside
``ProactorBasePipeTransport._call_connection_lost``. The previous
implementation wrapped the loop's exception handler — that didn't work
when Forge mounted our router after uvicorn's loop was already serving
(the ``startup`` event we registered never fired). Current
implementation attaches a ``logging.Filter`` to the ``asyncio`` logger,
which is global state and works regardless of mount order.
"""

from __future__ import annotations

import logging
import sys

import pytest

from modules.cplugapi import asyncio_filter


@pytest.fixture
def fresh_filter():
    """Reset the install state before/after each test so install() is
    actually exercised — module-level ``_INSTALLED`` is process-global
    and would short-circuit subsequent tests otherwise."""
    asyncio_filter._uninstall_for_tests()
    yield
    asyncio_filter._uninstall_for_tests()


def _make_record(exc, message: str) -> logging.LogRecord:
    """Build a LogRecord mimicking what asyncio's default handler emits.

    ``exc_info`` follows the ``logger.error(..., exc_info=...)`` shape:
    ``(type, value, traceback)``. The traceback is omitted (None) since
    the filter only inspects type + value.
    """
    record = logging.LogRecord(
        name="asyncio",
        level=logging.ERROR,
        pathname="<test>",
        lineno=0,
        msg=message,
        args=None,
        exc_info=(type(exc), exc, None),
    )
    return record


def test_drops_proactor_connection_reset():
    """The exact signature we want to mute: ConnectionResetError tagged
    with the _call_connection_lost marker. Filter returns False (drop)."""
    record = _make_record(
        ConnectionResetError("forcibly closed"),
        "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
    )
    assert asyncio_filter._filter_instance.filter(record) is False


def test_passes_unrelated_connection_reset():
    """A ConnectionResetError NOT from _call_connection_lost is genuine
    application error and must NOT be filtered."""
    record = _make_record(
        ConnectionResetError("some real bug"),
        "Exception in callback MyHandler.on_data()",
    )
    assert asyncio_filter._filter_instance.filter(record) is True


def test_passes_other_exception_in_connection_lost():
    """Even if the message names _call_connection_lost, a non-
    ConnectionResetError must not be filtered — that'd be a real bug
    we want to see."""
    record = _make_record(
        RuntimeError("not a peer-reset"),
        "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
    )
    assert asyncio_filter._filter_instance.filter(record) is True


def test_passes_record_without_exc_info():
    """Defensive: log records without exc_info (plain info messages)
    must always pass through. The filter only inspects exc_info."""
    record = logging.LogRecord(
        name="asyncio", level=logging.INFO, pathname="<test>", lineno=0,
        msg="something normal", args=None, exc_info=None,
    )
    assert asyncio_filter._filter_instance.filter(record) is True


@pytest.mark.skipif(sys.platform != "win32", reason="filter is Windows-only")
def test_install_attaches_filter_to_asyncio_logger(fresh_filter):
    """install() must add our filter instance to the asyncio logger so
    records emitted by the default exception handler get inspected."""
    asyncio_filter.install()
    asyncio_logger = logging.getLogger("asyncio")
    assert asyncio_filter._filter_instance in asyncio_logger.filters
    assert asyncio_filter.is_installed() is True


@pytest.mark.skipif(sys.platform != "win32", reason="filter is Windows-only")
def test_install_is_idempotent(fresh_filter):
    """Calling install twice must not double-attach the filter — the
    asyncio logger would otherwise grow an unbounded chain across
    webui reloads."""
    asyncio_filter.install()
    asyncio_filter.install()
    asyncio_logger = logging.getLogger("asyncio")
    occurrences = sum(1 for f in asyncio_logger.filters if f is asyncio_filter._filter_instance)
    assert occurrences == 1


@pytest.mark.skipif(sys.platform != "win32", reason="filter is Windows-only")
def test_install_accepts_app_for_back_compat(fresh_filter):
    """install(app) used to schedule a startup hook; now it ignores
    ``app`` entirely. Verify the call still works with an arbitrary
    object so router.py doesn't need to change every time we revisit
    this filter's strategy."""
    asyncio_filter.install(object())  # any non-None value
    assert asyncio_filter.is_installed() is True


@pytest.mark.skipif(sys.platform != "win32", reason="filter is Windows-only")
def test_real_asyncio_logger_suppresses_proactor_noise(fresh_filter, caplog):
    """End-to-end: a record emitted on the asyncio logger with the
    proactor signature is dropped before reaching handlers. This is the
    invariant that prevents the user-visible traceback spam."""
    asyncio_filter.install()
    asyncio_logger = logging.getLogger("asyncio")

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        try:
            raise ConnectionResetError("forcibly closed")
        except ConnectionResetError:
            asyncio_logger.error(
                "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
                exc_info=True,
            )

    # The proactor noise was filtered before reaching any handler.
    asyncio_records = [r for r in caplog.records if r.name == "asyncio"]
    assert asyncio_records == []


@pytest.mark.skipif(sys.platform != "win32", reason="filter is Windows-only")
def test_real_asyncio_logger_preserves_genuine_errors(fresh_filter, caplog):
    """Counterpart to the above: genuine asyncio errors (different
    handle, or different exception type) must still surface."""
    asyncio_filter.install()
    asyncio_logger = logging.getLogger("asyncio")

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        try:
            raise RuntimeError("genuine bug")
        except RuntimeError:
            asyncio_logger.error(
                "Exception in callback MyApp.handler()",
                exc_info=True,
            )

    asyncio_records = [r for r in caplog.records if r.name == "asyncio"]
    assert len(asyncio_records) == 1
    assert "genuine bug" in str(asyncio_records[0].exc_info[1])


def test_install_silent_on_non_windows():
    """On non-Windows platforms install() is a no-op — selector / kqueue
    loops don't surface this particular cleanup race."""
    if sys.platform == "win32":
        pytest.skip("Windows-specific behaviour tested elsewhere")
    asyncio_filter.install()
    assert asyncio_filter.is_installed() is False
