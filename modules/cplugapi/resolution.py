"""Headless resolution rounding for the cplugapi fork.

Upstream Forge-Neo rounds generation dimensions through
``modules.ui.sRound`` / ``modules.ui._STEP`` (see ``modules/ui.py``). Importing
those into ``modules.processing`` / ``modules.img2img`` drags Gradio into the
headless API path, which the fork must avoid (the ``/cplugapi`` + ``/sdapi``
server runs without the web UI imported).

This module reproduces upstream's behaviour *exactly* with no UI dependency:

    upstream ui.py:  _STEP = int(opts.res_step)
                     def sRound(val): return math.floor(val / _STEP + 0.5) * _STEP

The ``res_step`` option is ``.needs_restart()`` upstream, so the step is read
once and cached for the process lifetime. The read is lazy (first use) rather
than at import time, because ``modules.processing`` is imported earlier in
bootstrap than the options registry is fully populated; a lazy read avoids a
stale/raising read while still being fixed for the session.
"""

import math

from modules.shared import opts

_step_cache: "int | None" = None


def step() -> int:
    """The configured resolution step (``opts.res_step``), read once and cached.

    Falls back to 64 (the upstream default) if the option is not yet
    registered or is unreadable.
    """
    global _step_cache
    if _step_cache is None:
        try:
            _step_cache = int(getattr(opts, "res_step", 64) or 64)
        except Exception:
            _step_cache = 64
    return _step_cache


def sRound(val: "int | float") -> int:
    """Round ``val`` to the nearest multiple of :func:`step`.

    Byte-identical to upstream ``modules.ui.sRound`` for any given step, so at
    ``res_step == 64`` it reproduces upstream integers at every call site.
    """
    s = step()
    return int(math.floor(val / s + 0.5) * s)
