"""Demote benign Windows asyncio connection-reset noise to DEBUG.

Background
----------
asyncio's Windows ``ProactorEventLoop`` calls
``ProactorBasePipeTransport._call_connection_lost`` when a TCP
transport closes. That callback tries ``self._sock.shutdown(SHUT_RDWR)``
to drain any pending data — but if the peer already sent RST (a hard
forcible close, common when the desktop ControlPlugin client preempts
an in-flight generation for a fresh sketch stroke), the kernel returns
``WinError 10054`` (ConnectionResetError). Python's default asyncio
exception handler logs this loudly as ``Exception in callback…`` even
though there is nothing to do — the connection is already gone.

The traceback is *cosmetic*: nothing fails, the in-progress request
completes normally, no work is lost. Under the live-sketching workflow
where the client routinely closes connections to start a new gen,
these tracebacks dominate the log and drown out real signal.

Mitigation
----------
Wrap the running loop's exception handler with a filter that
recognises this specific signature (ConnectionResetError raised
inside ``_call_connection_lost``) and either silences it or demotes
it to DEBUG. Anything we don't recognise goes through the original
handler unchanged so genuine errors stay visible.

The filter is Windows-only (other platforms use SelectorEventLoop /
KqueueEventLoop and don't surface this particular noise) and idempotent
(calling install twice is a no-op).
"""

from __future__ import annotations

import logging
import sys
import threading

_log = logging.getLogger("cplugapi.asyncio_filter")
try:
    from backend.logging import setup_logger as _setup_logger
    _setup_logger(_log)
except ImportError:
    pass

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
# Marker added to loops we've already wrapped so a second install attempt
# (test reuse, webui reload) doesn't double-wrap. Using a ``setattr`` on
# the loop is robust across loop instances; a module-level flag would
# miss cases where uvicorn replaces the loop on reload.
_LOOP_WRAPPED_ATTR = "_cplug_asyncio_filter_installed"


def _is_proactor_connection_reset(context: dict) -> bool:
    """True for the specific Windows asyncio cleanup race we want to mute.

    Two signals must both match:
    - The exception is a ``ConnectionResetError`` (or its WinError-10054
      variant — they share the same Python type).
    - The handle's repr names ``_call_connection_lost`` (the
      ProactorBasePipeTransport cleanup path). We match on repr rather
      than importing the private class because the import path differs
      across asyncio versions; repr matching is stable enough for a
      defensive filter.
    """
    exc = context.get("exception")
    if not isinstance(exc, ConnectionResetError):
        return False
    handle = context.get("handle")
    if handle is None:
        return False
    return "_call_connection_lost" in repr(handle)


def _make_filtered_handler(original):
    """Build an exception handler that swallows the proactor noise and
    delegates everything else to ``original``.

    ``original`` may be ``None`` (loop has no custom handler) — in that
    case unrecognised exceptions go through ``loop.default_exception_handler``.
    """

    def handler(loop, context):
        if _is_proactor_connection_reset(context):
            # DEBUG so verbose-mode operators can still inspect them,
            # but the default INFO console stays clean.
            _log.debug(
                "asyncio cleanup ConnectionResetError suppressed: %s",
                context.get("message", ""),
            )
            return
        if original is not None:
            original(loop, context)
        else:
            loop.default_exception_handler(context)

    return handler


def _install_on_running_loop() -> None:
    """Wrap the running loop's exception handler. Idempotent.

    Must be called from inside a running loop (either coroutine context
    or Starlette's startup hook) — :func:`install` arranges that.
    """
    global _INSTALLED
    if sys.platform != "win32":
        return

    import asyncio

    with _INSTALL_LOCK:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Not in a running loop. Caller is misusing the API; silent
            # rather than raising so a bad call site can't take the app
            # down.
            return

        if getattr(loop, _LOOP_WRAPPED_ATTR, False):
            return

        original = loop.get_exception_handler()
        loop.set_exception_handler(_make_filtered_handler(original))
        setattr(loop, _LOOP_WRAPPED_ATTR, True)
        _INSTALLED = True


def install(app=None) -> None:
    """Schedule the filter to install on the app's running event loop.

    Why deferred: ``setup_cplugapi`` runs during route mounting, which
    is a sync context BEFORE uvicorn boots its loop. ``asyncio.
    get_event_loop()`` from there returns a freshly-created throwaway
    loop in Python 3.12+ — wrapping its handler does nothing because
    uvicorn binds a different loop at startup. We register a startup
    event handler instead, so installation runs inside the actual
    serving loop and ``asyncio.get_running_loop()`` is well-defined.

    Windows-only — other platforms don't surface the proactor cleanup
    race. ``app=None`` is accepted for the test path that wants to
    drive ``_install_on_running_loop`` directly from a coroutine.
    """
    if sys.platform != "win32":
        return

    if app is None:
        # Test path: caller (a coroutine) drives installation directly.
        _install_on_running_loop()
        return

    async def _on_startup() -> None:
        _install_on_running_loop()

    app.add_event_handler("startup", _on_startup)


def is_installed() -> bool:
    """Test-only: surface whether the filter has been wired."""
    return _INSTALLED
