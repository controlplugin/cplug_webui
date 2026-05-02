"""``torch.compile`` MegaCache management (audit 02 Phase C).

PyTorch 2.7+ exposes ``torch.compiler.save_cache_artifacts`` /
``load_cache_artifacts`` — a single-shot bundle of the FX graph,
autograd, and inductor caches keyed to the running device. For
ControlPlugin's live-sketching workload the relevant compile passes
fire on the first stroke; replaying a saved bundle on the next launch
elides the bulk of that latency.

The module is best-effort:

* never raises into the caller — every step is wrapped in
  ``try/except`` and degrades to a WARNING log;
* idempotent across :func:`apply` invocations;
* safe to skip on PyTorch < 2.7 (the symbols are absent — module
  reports the cap predicate as false and returns).

Repo root is resolved once at import time by walking up from this
file's location until a ``webui.py`` sibling or ``.git/`` directory
is found. Cached at module level so subsequent calls are O(1).
"""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


def _resolve_repo_root() -> Path:
    """Walk up from this file until a sibling ``webui.py`` or ``.git/`` shows up.

    Falls back to the parent-of-parent-of-this-file if neither marker
    exists (degenerate test environment), which still keeps the cache
    inside the package tree rather than in cwd.
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "webui.py").is_file():
            return candidate
        if (candidate / ".git").exists():
            return candidate
    return here.parent.parent.parent


_REPO_ROOT: Path = _resolve_repo_root()

_CACHE_FILENAME = "megacache.bin"
_LOADED_OK = False
_ATEXIT_INSTALLED = False
_APPLIED = False


def _cache_dir() -> Path:
    return _REPO_ROOT / "cache" / "inductor"


def _cache_file() -> Path:
    return _cache_dir() / _CACHE_FILENAME


def configure_env() -> None:
    """Best-effort env defaults for the inductor caches.

    Sets ``TORCHINDUCTOR_FX_GRAPH_CACHE`` and
    ``TORCHINDUCTOR_AUTOGRAD_CACHE`` to ``1`` and points
    ``TORCHINDUCTOR_CACHE_DIR`` at ``<repo_root>/cache/inductor/`` —
    each only when the variable is unset, so an operator override
    always wins. Idempotent.
    """
    os.environ.setdefault("TORCHINDUCTOR_FX_GRAPH_CACHE", "1")
    os.environ.setdefault("TORCHINDUCTOR_AUTOGRAD_CACHE", "1")
    os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", str(_cache_dir()))


def load_artifacts() -> bool:
    """Replay a previously saved MegaCache bundle if one exists.

    Returns True when the bundle was found and successfully loaded,
    False otherwise (no file, PyTorch < 2.7, or load raised). Never
    raises — the live-sketching launch path must not fail because of
    a stale cache.
    """
    global _LOADED_OK
    cache_file = _cache_file()
    if not cache_file.is_file():
        return False
    try:
        import torch
    except ImportError:
        return False
    loader = getattr(getattr(torch, "compiler", None), "load_cache_artifacts", None)
    if loader is None:
        return False
    try:
        with open(cache_file, "rb") as fh:
            payload = fh.read()
        loader(payload)
    except Exception as exc:
        _log.warning("cplugapi: MegaCache load failed (%s)", exc)
        return False
    _LOADED_OK = True
    _log.info("cplugapi: MegaCache loaded from %s", cache_file)
    return True


def save_artifacts() -> bool:
    """Persist the in-memory MegaCache bundle to disk.

    Returns True when bytes were written, False on any failure
    (PyTorch < 2.7, no artifacts to save, IO error). Never raises.
    """
    try:
        import torch
    except ImportError:
        return False
    saver = getattr(getattr(torch, "compiler", None), "save_cache_artifacts", None)
    if saver is None:
        return False
    try:
        result = saver()
    except Exception as exc:
        _log.warning("cplugapi: MegaCache save failed (%s)", exc)
        return False
    if result is None:
        return False
    # PyTorch 2.7 returns either raw bytes or a (bytes, info) tuple
    # depending on the patch level — accept both shapes.
    payload: Optional[bytes]
    if isinstance(result, tuple):
        payload = result[0] if result else None
    else:
        payload = result
    if not payload:
        return False
    try:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / _CACHE_FILENAME
        with open(cache_file, "wb") as fh:
            fh.write(payload)
    except OSError as exc:
        _log.warning("cplugapi: MegaCache write failed (%s)", exc)
        return False
    _log.info("cplugapi: MegaCache saved to %s (%d bytes)", cache_file, len(payload))
    return True


def install_atexit() -> None:
    """Register :func:`save_artifacts` to run at interpreter shutdown.

    Idempotent — a module-level flag prevents duplicate registrations
    across repeated :func:`apply` calls in the same process.
    """
    global _ATEXIT_INSTALLED
    if _ATEXIT_INSTALLED:
        return
    atexit.register(save_artifacts)
    _ATEXIT_INSTALLED = True


def apply() -> None:
    """Configure env, attempt a load, and arm the at-exit save. Idempotent."""
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True
    configure_env()
    load_artifacts()
    install_atexit()


def loaded_ok() -> bool:
    """Predicate for the ``runtime/megacache`` capability.

    True iff :func:`load_artifacts` reported a hit during the current
    process — i.e. a cached bundle was found *and* successfully
    deserialized by torch. Operators reading
    ``/cplugapi/v1/health.capabilities[]`` use this as the signal that
    the next session will get warm-cache compile latency.
    """
    return _LOADED_OK


def register_capabilities() -> None:
    """Register the ``runtime/megacache`` capability.

    Predicate fires when the on-disk bundle was loaded successfully
    on this process's startup path.
    """
    from modules.cplugapi import capabilities

    capabilities.register("runtime/megacache", predicate=loaded_ok)
