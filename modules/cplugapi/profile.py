"""``CPLUG_DEPLOYMENT_PROFILE`` — ``desktop`` | ``cloud`` profile selector.

The fork's primary deployment is desktop loopback (single user behind
``--api-auth`` on ``127.0.0.1``). The secondary deployment is cloud
single-replica behind an ingress (TLS termination + auth at the proxy,
fork bound to ``0.0.0.0``). The two postures want different
**defaults** for several knobs:

================  =================  =================
Knob              ``desktop``        ``cloud``
================  =================  =================
ALLOWED_HOSTS     loopback only      any non-empty (``*``)
ALLOWED_ORIGINS   loopback regex     wildcard (``*``)
auto_preempt      ``always``         ``off``
rate-limit (W8)   all classes off    all classes on
================  =================  =================

Setting an explicit env var (``CPLUG_ALLOWED_HOSTS``, etc.) always
overrides the profile default. Profile is read once at install time;
toggling requires a webui restart, like every other launcher option.

Multi-replica is **explicitly out of scope** — see ``§1`` non-goals
in ``plan/cplugapi-world-class.md``. Cloud profile assumes
single-replica behind an ingress; cross-replica session state and
distributed rate-limit state are not addressed here.
"""

from __future__ import annotations

import logging
import os

from . import capabilities

_log = logging.getLogger(__name__)

ENV_PROFILE = "CPLUG_DEPLOYMENT_PROFILE"
PROFILE_DESKTOP = "desktop"
PROFILE_CLOUD = "cloud"
DEFAULT_PROFILE = PROFILE_DESKTOP
_VALID: frozenset[str] = frozenset({PROFILE_DESKTOP, PROFILE_CLOUD})


def get_profile() -> str:
    """Read ``CPLUG_DEPLOYMENT_PROFILE``. Returns the validated value
    or :data:`DEFAULT_PROFILE` for unset / unrecognised input.

    Re-read on every call rather than cached — tests monkeypatch the
    env var directly and the cost is one ``os.environ.get`` lookup.
    Modules that need a single canonical value across an install call
    :func:`get_profile` once and stash the result.
    """
    raw = os.environ.get(ENV_PROFILE, "").strip().lower()
    if not raw:
        return DEFAULT_PROFILE
    if raw in _VALID:
        return raw
    _log.warning(
        "%s=%r not recognised; falling back to %r. Valid: %s",
        ENV_PROFILE,
        raw,
        DEFAULT_PROFILE,
        sorted(_VALID),
    )
    return DEFAULT_PROFILE


def is_cloud() -> bool:
    """Convenience predicate. ``True`` when profile is ``cloud``."""
    return get_profile() == PROFILE_CLOUD


def is_desktop() -> bool:
    """Convenience predicate. ``True`` when profile is ``desktop``."""
    return get_profile() == PROFILE_DESKTOP


def register_capabilities() -> None:
    """Advertise the active profile when it's not the default.

    ``deployment-profile-cloud`` only registers when cloud is active —
    its presence is the signal to clients/operators. Desktop is the
    default and gets no capability string (absence is the signal).
    """
    if is_cloud():
        capabilities.register("deployment-profile-cloud")
