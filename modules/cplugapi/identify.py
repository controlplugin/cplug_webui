"""``GET /cplugapi/v1/identify`` — Track 05 §5.1.

The cheapest possible probe: lets the desktop client distinguish the
ControlPlugin_WebUI fork from upstream Forge Neo / Reforge / vanilla A1111
without inflicting state on the backend. **Unauthenticated** — bootstrap
chicken-and-egg means the client must identify before it knows whether to
send credentials.

W4: surface ``capabilities[]`` here too so a client can negotiate
features without first authenticating to ``/health``. Capability
strings are filtered through :func:`_safe_capability` — today's
strings are slash-only lowercase identifiers (no leak vector), but
the filter is a forward guard against a future capability that
embeds a checkpoint filename or commit SHA.
"""

from __future__ import annotations

import re

from fastapi import APIRouter

from . import capabilities as _capabilities
from .__version__ import (
    FORK_COMMIT,
    FORK_NAME,
    FORK_VERSION,
    UPSTREAM_COMMIT,
    UPSTREAM_NAME,
)

# Forward-guard regexes — applied to every capability string before it
# leaves the unauthenticated /identify endpoint. A capability whose
# *string itself* matches any of these patterns is filtered out (it
# stays on /health post-auth, where revealing more is acceptable).
#
# Today no capability matches these patterns; the filter is here to
# prevent a future "checkpoint-loaded/sd_xl_base_1.0" or
# "build/<commit-sha>" capability from accidentally leaking via the
# public probe.

# Bare hex SHA (7-40 chars). git short-shas start at 7; full SHAs are 40.
_LOOKS_LIKE_HASH = re.compile(r"^[a-f0-9]{7,40}$", re.IGNORECASE)

# Capability strings ending in a model-file extension — any path
# segment with these suffixes is a checkpoint name leaking.
_CHECKPOINT_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")


def _safe_capability(name: str) -> bool:
    """Return True if ``name`` is safe to expose unauthenticated."""
    if _LOOKS_LIKE_HASH.match(name):
        return False
    lower = name.lower()
    for suffix in _CHECKPOINT_SUFFIXES:
        if lower.endswith(suffix):
            return False
    return True


def _public_capabilities() -> list[str]:
    """Filtered, sorted list of capabilities safe for public exposure."""
    return [c for c in _capabilities.enabled_capabilities() if _safe_capability(c)]


def _public_deprecated() -> list[str]:
    """Filter ``capabilities.deprecated_capabilities()`` through the
    same safety predicate used for ``_public_capabilities`` so nothing
    leaky surfaces in the deprecation announcement."""
    return [
        c for c in _capabilities.deprecated_capabilities() if _safe_capability(c)
    ]


def attach(router: APIRouter) -> None:
    @router.get("/identify")
    def identify() -> dict:
        return {
            "fork": FORK_NAME,
            "fork_version": FORK_VERSION,
            "fork_commit": FORK_COMMIT,
            "upstream": UPSTREAM_NAME,
            "upstream_commit": UPSTREAM_COMMIT,
            "capabilities": _public_capabilities(),
            "deprecated_capabilities": _public_deprecated(),
        }
