"""Capability registry — single source of truth for ``/cplugapi/v1/health.capabilities[]``.

Capability strings follow the slash-only ``<area>/<feature>`` convention
documented in the capability registry §1 (track 00 foundation).
Dot notation (``transport.base64``) is treated as a registration error.

Endpoint modules call :func:`register` at attach time. The client gates
fork-specific features on :func:`enabled_capabilities`.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

Predicate = Callable[[], bool]

_log = logging.getLogger(__name__)

_lock = threading.Lock()
_registry: dict[str, Predicate] = {}
# W15 — capability strings scheduled for removal in the next minor.
# Both the legacy string and its replacement are simultaneously
# registered (dual-emission window); this set tracks the legacy
# strings so ``/health`` and ``/identify`` can publish a
# ``deprecated_capabilities[]`` array — the Rust client gets a
# release cycle to migrate, then the legacy string is dropped.
_deprecated: set[str] = set()


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


def register_with_legacy(
    new_name: str,
    legacy_name: str,
    predicate: Optional[Predicate] = None,
) -> None:
    """Register both a new namespaced name and a legacy alias (W15).

    Both strings appear on ``/health.capabilities[]`` and
    ``/identify.capabilities[]``. The legacy name is additionally
    tracked in :func:`deprecated_capabilities` so clients can detect
    the upcoming removal.

    Use this for fork-local capability strings that are being
    re-namespaced (e.g. ``request-log`` → ``observability/request-log``).
    Canonical capability strings (those listed in the project's
    capability registry) MUST NOT change names — call :func:`register`
    for those.
    """
    if new_name == legacy_name:
        raise ValueError(
            f"register_with_legacy: new_name and legacy_name must differ "
            f"(both are {new_name!r}). For a single name with no rename, "
            "call register() directly."
        )
    register(new_name, predicate)
    register(legacy_name, predicate)
    with _lock:
        _deprecated.add(legacy_name)


def unregister(name: str) -> None:
    """Remove a capability. No-op if absent. Primarily for tests."""
    with _lock:
        _registry.pop(name, None)
        _deprecated.discard(name)


def deprecated_capabilities() -> list[str]:
    """Return the sorted list of legacy capability strings scheduled
    for removal. Surfaces on ``/health`` and ``/identify`` so clients
    can detect the deprecation window."""
    with _lock:
        return sorted(_deprecated)


def enabled_capabilities() -> list[str]:
    """Return capabilities whose predicates evaluate true, sorted ascending."""
    with _lock:
        items = list(_registry.items())
    enabled = []
    for name, predicate in items:
        try:
            if predicate():
                enabled.append(name)
        except Exception as exc:
            # A misbehaving predicate must not poison /health, but it
            # should be visible to operators so the silent absence of a
            # capability can be diagnosed.
            _log.warning("cplugapi capability predicate %r failed: %s", name, exc)
            continue
    enabled.sort()
    return enabled


def reset() -> None:
    """Clear the registry. Test-only."""
    with _lock:
        _registry.clear()
        _deprecated.clear()
