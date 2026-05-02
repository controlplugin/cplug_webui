"""ControlPlugin WebUI fork-specific API surface (`/cplugapi/v1/*`).

All fork-only endpoints live under this package so upstream rebases against
``Haoming02/sd-webui-forge-classic@neo`` only need to touch one line in
``modules/api/api.py`` (the ``setup_cplugapi`` call).

Spec: track 05 (cplugapi-v1) of the sibling planning repo.
"""

from .__version__ import (
    FORK_BUILD_DATE,
    FORK_COMMIT,
    FORK_NAME,
    FORK_VERSION,
    UPSTREAM_BRANCH,
    UPSTREAM_COMMIT,
    UPSTREAM_NAME,
)
from .router import PREFIX, setup_cplugapi

__all__ = [
    "FORK_BUILD_DATE",
    "FORK_COMMIT",
    "FORK_NAME",
    "FORK_VERSION",
    "PREFIX",
    "UPSTREAM_BRANCH",
    "UPSTREAM_COMMIT",
    "UPSTREAM_NAME",
    "setup_cplugapi",
]
