"""Fork + upstream version constants.

Build-time commit SHAs are read from environment variables that CI is
expected to set via ``git rev-parse --short HEAD`` (see Track 05 T19). When
the env vars are absent (local dev) the value falls back to ``"unknown"``.
"""

import os

FORK_NAME = "controlplugin_webui"
FORK_VERSION = "0.1.0"
UPSTREAM_NAME = "forge-neo"

FORK_COMMIT = os.environ.get("CPLUG_FORK_COMMIT", "unknown")
UPSTREAM_COMMIT = os.environ.get("CPLUG_UPSTREAM_COMMIT", "unknown")
