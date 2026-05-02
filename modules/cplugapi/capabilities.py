"""Capability registry — single source of truth for ``/cplugapi/v1/health.capabilities[]``.

Capability strings follow the slash-only ``<area>/<feature>`` convention
documented in
``ControlPlugin_WebUI/plan/00-foundation/04-capability-registry.md`` §1.
Dot notation (``transport.base64``) is treated as a registration error.

Endpoint modules call :func:`register` at attach time. The client gates
fork-specific features on :func:`enabled_capabilities`.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

Predicate = Callable[[], bool]

_lock = threading.Lock()
_registry: dict[str, Predicate] = {}


def register(name: str, predicate: Optional[Predicate] = None) -> None:
    """Register a capability string. ``predicate=None`` means always-on.

    Re-registration replaces the predicate (idempotent — useful for tests).
    """
    if "." in name:
        raise ValueError(
            f"Capability {name!r} uses dot notation; slash-only is the locked "
            "convention (see plan/00-foundation/04-capability-registry.md §6)."
        )
    if not name or name != name.strip():
        raise ValueError(f"Capability name must be non-empty and untrimmed: {name!r}")
    with _lock:
        _registry[name] = predicate or (lambda: True)


def unregister(name: str) -> None:
    """Remove a capability. No-op if absent. Primarily for tests."""
    with _lock:
        _registry.pop(name, None)


def enabled_capabilities() -> list[str]:
    """Return capabilities whose predicates evaluate true, sorted ascending."""
    with _lock:
        items = list(_registry.items())
    enabled = []
    for name, predicate in items:
        try:
            if predicate():
                enabled.append(name)
        except Exception:
            # A misbehaving predicate must not poison /health.
            continue
    enabled.sort()
    return enabled


def reset() -> None:
    """Clear the registry. Test-only."""
    with _lock:
        _registry.clear()
