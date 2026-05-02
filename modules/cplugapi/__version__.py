"""Fork + upstream version constants.

Build-time SHAs and the build date are read from environment variables
that CI is expected to set (see Track 05 T19). When the env vars are
absent (local dev), ``FORK_COMMIT``/``UPSTREAM_COMMIT`` fall back to
``"unknown"`` and ``FORK_BUILD_DATE`` falls back to the moment this module
is first imported (process startup, *not* request time).
"""

from __future__ import annotations

import datetime
import os

FORK_NAME = "controlplugin_webui"
FORK_VERSION = "0.1.0"
UPSTREAM_NAME = "forge-neo"
UPSTREAM_BRANCH = "neo"

FORK_COMMIT = os.environ.get("CPLUG_FORK_COMMIT", "unknown")
UPSTREAM_COMMIT = os.environ.get("CPLUG_UPSTREAM_COMMIT", "unknown")

FORK_BUILD_DATE = os.environ.get(
    "CPLUG_FORK_BUILD_DATE",
    datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
)
