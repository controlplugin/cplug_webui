"""Demote benign Windows asyncio connection-reset noise to DEBUG.

Background
----------
asyncio's Windows ``ProactorEventLoop`` calls
``ProactorBasePipeTransport._call_connection_lost`` when a TCP
transport closes. That callback tries ``self._sock.shutdown(SHUT_RDWR)``
to drain any pending data — but if the peer already sent RST (a hard
forcible close, common when the desktop ControlPlugin client preempts
an in-flight generation for a fresh sketch stroke), the kernel returns
``WinError 10054`` (ConnectionResetError). The default asyncio
exception handler logs this loudly as ``Exception in callback…`` even
though there is nothing to do — the connection is already gone.

The traceback is *cosmetic*: nothing fails, the in-progress request
completes normally, no work is lost. Under the live-sketching workflow
where the client routinely closes connections to start a new gen,
these tracebacks dominate the log and drown out real signal.

Mitigation
----------
asyncio's default exception handler emits via
``logging.getLogger("asyncio").error(...)``. We attach a
:class:`logging.Filter` to that logger and drop records whose
``exc_info`` is a ``ConnectionResetError`` raised inside
``_call_connection_lost``.

Why a logging filter rather than ``loop.set_exception_handler``:
Forge mounts our router AFTER uvicorn's loop is already serving, so a
``startup`` event hook never fires. ``asyncio.get_event_loop()`` from
our sync mount context returns a throwaway loop, not the serving one.
The logger is global state — installation is a dict update, no loop
coordination needed, and any loop that uses the default handler routes
through the same logger. Bullet-proof against the mount-ordering
problem.

Windows-only and idempotent.
"""

from __future__ import annotations

import logging
import re
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

# Substring that uniquely identifies the proactor cleanup path. Match
# against the message rather than importing the private class — the
# import path differs across asyncio versions, and the message text
# is stable (``Exception in callback _ProactorBasePipeTransport.
# _call_connection_lost()``).
_PROACTOR_MESSAGE_MARKER = "_ProactorBasePipeTransport._call_connection_lost"
_HANDLE_REPR_MARKER = re.compile(r"_call_connection_lost")


class _ProactorResetFilter(logging.Filter):
    """Drop the specific Windows asyncio cleanup race we want to mute.

    Two signals must both match before the record is suppressed:

    - ``record.exc_info`` is (or wraps) a ``ConnectionResetError``.
    - The message names ``_call_connection_lost`` (the
      ``ProactorBasePipeTransport`` cleanup path).

    Anything else passes through untouched so genuine asyncio errors
    (real ConnectionResetError elsewhere, RuntimeErrors, etc.) stay
    visible at their original level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        if not exc_info or not isinstance(exc_info, tuple) or len(exc_info) < 2:
            return True
        exc = exc_info[1]
        if not isinstance(exc, ConnectionResetError):
            return True

        # Cheap content sniff: the default handler builds the message
        # by joining the context dict's values, so the marker shows up
        # in either ``record.msg`` or ``record.getMessage()``. We also
        # accept a handle-repr marker for handlers that format
        # differently.
        message = record.getMessage()
        if _PROACTOR_MESSAGE_MARKER in message or _HANDLE_REPR_MARKER.search(message):
            # DEBUG so verbose-mode operators can still inspect them,
            # but the default INFO console stays clean.
            _log.debug(
                "asyncio cleanup ConnectionResetError suppressed: %s",
                message.splitlines()[0] if message else "<no message>",
            )
            return False

        return True


_filter_instance = _ProactorResetFilter()


def install(app=None) -> None:
    """Attach the proactor-noise filter to the ``asyncio`` logger.

    ``app`` is accepted but ignored — kept for backward compatibility
    with the previous loop-handler-based API. Installation is global
    logger state; no per-app or per-loop wiring required.

    Idempotent: subsequent calls are no-ops. Windows-only — other
    platforms don't surface this particular cleanup race.
    """
    global _INSTALLED
    if sys.platform != "win32":
        return

    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        logging.getLogger("asyncio").addFilter(_filter_instance)
        _INSTALLED = True


def is_installed() -> bool:
    """Test-only: surface whether the filter has been wired."""
    return _INSTALLED


def _uninstall_for_tests() -> None:
    """Remove the filter — exists so tests can reset module state.

    Not part of the public API; production has no use for unwiring.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        logging.getLogger("asyncio").removeFilter(_filter_instance)
        _INSTALLED = False
