"""Tests for ``modules.cplugapi.asyncio_filter``.

The filter targets a specific Windows asyncio cleanup race:
``ConnectionResetError`` raised inside
``ProactorBasePipeTransport._call_connection_lost``. We verify the
matching logic at the unit level (no real loop required) and the
installation wrapper at integration level (with a fresh asyncio loop).
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from modules.cplugapi import asyncio_filter


def _make_context(exc, handle_repr: str) -> dict:
    """Build a fake asyncio exception-handler context dict.

    asyncio's real handler receives ``{"exception": ..., "handle": ...,
    "message": ...}`` — we only populate the fields the filter inspects.
    """
    handle = MagicMock()
    handle.__repr__ = lambda self: handle_repr
    return {"exception": exc, "handle": handle, "message": "test"}


def test_recognises_proactor_connection_reset():
    """The exact signature we want to mute: ConnectionResetError raised
    inside _call_connection_lost. Both signals must match."""
    ctx = _make_context(
        ConnectionResetError("forcibly closed"),
        handle_repr="<Handle ProactorBasePipeTransport._call_connection_lost()>",
    )
    assert asyncio_filter._is_proactor_connection_reset(ctx) is True


def test_unrelated_connection_reset_passes_through():
    """A ConnectionResetError NOT from _call_connection_lost is genuine
    application error and must NOT be filtered."""
    ctx = _make_context(
        ConnectionResetError("some real bug"),
        handle_repr="<Handle MyHandler.on_data()>",
    )
    assert asyncio_filter._is_proactor_connection_reset(ctx) is False


def test_other_exception_in_connection_lost_passes_through():
    """Even if it's _call_connection_lost, a non-ConnectionResetError
    must not be filtered — that'd be a real bug we want to see."""
    ctx = _make_context(
        RuntimeError("not a peer-reset"),
        handle_repr="<Handle ProactorBasePipeTransport._call_connection_lost()>",
    )
    assert asyncio_filter._is_proactor_connection_reset(ctx) is False


def test_missing_handle_is_safe():
    """Defensive: if context doesn't carry a handle (synthetic test
    contexts can lack it), the filter must return False not raise."""
    assert asyncio_filter._is_proactor_connection_reset(
        {"exception": ConnectionResetError("x"), "message": "test"}
    ) is False


def test_filtered_handler_swallows_proactor_reset_and_delegates_others():
    """Handler factory composition — proactor noise is suppressed,
    everything else falls through to the original."""
    original_calls = []

    def original(loop, context):
        original_calls.append(context)

    handler = asyncio_filter._make_filtered_handler(original)

    # Proactor noise: not delegated.
    handler(
        MagicMock(),
        _make_context(
            ConnectionResetError("x"),
            "<Handle ProactorBasePipeTransport._call_connection_lost()>",
        ),
    )
    assert original_calls == []

    # Real error: delegated.
    real_ctx = _make_context(RuntimeError("real"), "<Handle MyApp.boom()>")
    handler(MagicMock(), real_ctx)
    assert original_calls == [real_ctx]


def test_filtered_handler_falls_back_to_default_when_no_original():
    """If the loop had no custom handler, unrecognised exceptions must
    go through ``loop.default_exception_handler``."""
    handler = asyncio_filter._make_filtered_handler(None)

    loop = MagicMock()
    real_ctx = _make_context(RuntimeError("real"), "<Handle MyApp.boom()>")
    handler(loop, real_ctx)
    loop.default_exception_handler.assert_called_once_with(real_ctx)


@pytest.mark.skipif(sys.platform != "win32", reason="filter is Windows-only")
def test_install_is_idempotent_on_windows():
    """Calling install twice on the same running loop must not
    double-wrap. Otherwise each install adds another layer and the
    chain grows unboundedly across webui reloads."""

    async def _drive():
        asyncio_filter._install_on_running_loop()
        first = asyncio.get_running_loop().get_exception_handler()
        asyncio_filter._install_on_running_loop()
        second = asyncio.get_running_loop().get_exception_handler()
        assert first is second
        assert first is not None  # actually wrapped, not the default

    asyncio.run(_drive())


@pytest.mark.skipif(sys.platform != "win32", reason="filter is Windows-only")
def test_install_via_app_defers_to_startup_event():
    """``install(app)`` must NOT touch the loop synchronously — uvicorn's
    real loop isn't reachable yet at route-mount time. Instead it
    registers a startup hook that runs once the loop is live."""
    from starlette.applications import Starlette

    app = Starlette()
    handlers_before = list(app.router.on_startup)
    asyncio_filter.install(app)
    handlers_after = list(app.router.on_startup)
    # Exactly one new startup handler was registered.
    assert len(handlers_after) == len(handlers_before) + 1


def test_install_silent_on_non_windows():
    """On non-Windows platforms the function is a no-op — the underlying
    selector/kqueue loops don't have this particular issue."""
    if sys.platform == "win32":
        pytest.skip("Windows-specific behaviour tested elsewhere")
    asyncio_filter.install()
    # No assertion needed — the test passes if install() didn't raise.
