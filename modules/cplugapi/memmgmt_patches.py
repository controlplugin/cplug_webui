"""Defensive monkey-patches for ``backend/memory_management.py``
(audit 02 Phase C).

Targets upstream issue Forge-Neo #694 (closed-as-dup #1017): under
repeated checkpoint swaps in long-running cplugapi sessions,
``LoadedModel.is_dead()`` crashes once ``self.real_model`` has been
reassigned to ``None`` (the sentinel set by ``model_unload`` and
``__init__``).

Upstream shape (verified at audit time against
``backend/memory_management.py`` revision currently on the ``neo``
branch):

    class LoadedModel:
        def __init__(self, model):
            ...
            self.real_model = None        # <-- sentinel
            ...

        def model_load(self, ...):
            ...
            self.real_model = weakref.ref(real_model)  # <-- weakref
            ...

        def model_unload(self, ...):
            ...
            self.real_model = None        # <-- back to sentinel
            ...

        def is_dead(self):
            return self.real_model() is not None and self.model is None

When ``self.real_model`` is the bare ``None`` sentinel, the
``self.real_model()`` call in ``is_dead`` raises
``TypeError: 'NoneType' object is not callable``. The wrapper here
short-circuits that case and reports the model as dead, matching the
spirit of the original (an unloaded slot *is* dead and must be
collected by ``free_memory`` / ``cleanup_models``).

The patch lives outside ``backend/`` to keep the upstream rebase
surface clean — :func:`apply` rebinds the method on the class
in-place at fork-bootstrap time. Idempotent via a class-level
``_cplugapi_isdead_patched`` sentinel attribute.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_PATCHED_FLAG = "_cplugapi_isdead_patched"


def _resolve_default_class():
    """Import and return ``backend.memory_management.LoadedModel``.

    Returns ``None`` when the backend is not importable (typical
    inside the cplugapi unit test environment, where importing
    ``backend.memory_management`` would pull in torch + the full
    args parser). Callers must treat ``None`` as a no-op signal,
    not an error.
    """
    try:
        from backend import memory_management  # type: ignore
    except Exception:
        return None
    return getattr(memory_management, "LoadedModel", None)


def apply(cls=None) -> bool:
    """Wrap ``cls.is_dead`` so a ``None`` ``real_model`` returns True
    instead of crashing (upstream issue #694).

    Parameters
    ----------
    cls:
        The class whose ``is_dead`` method should be patched. When
        ``None`` (the default) the real
        ``backend.memory_management.LoadedModel`` is resolved at call
        time. Tests pass an arbitrary fake-shaped class to exercise
        the wrapper without dragging in the backend module.

    Returns
    -------
    bool
        True when this call newly installed the wrapper. False when
        the patch was already in place (idempotent re-application) or
        the target class could not be resolved (degenerate
        environment — caller may still proceed; the bug only matters
        once the backend is loaded).
    """
    if cls is None:
        cls = _resolve_default_class()
    if cls is None:
        return False
    if getattr(cls, _PATCHED_FLAG, False):
        return False

    original = cls.is_dead

    def _patched_is_dead(self) -> bool:
        rm = getattr(self, "real_model", None)
        # ``real_model`` is either a weakref (callable) or the bare
        # ``None`` sentinel set by ``model_unload`` / ``__init__``.
        # The crash path is the latter — short-circuit it.
        if rm is None:
            return True
        try:
            return original(self)
        except TypeError:
            # Defense-in-depth: if a future upstream refactor leaves
            # some other non-callable in ``real_model``, do not
            # propagate the crash through ``free_memory``.
            return True

    _patched_is_dead.__name__ = "is_dead"
    _patched_is_dead.__qualname__ = f"{cls.__qualname__}.is_dead"
    _patched_is_dead.__doc__ = (
        "is_dead() guarded against real_model=None (upstream Forge-Neo #694)."
    )

    cls.is_dead = _patched_is_dead
    setattr(cls, _PATCHED_FLAG, True)
    _log.info(
        "cplugapi: patched %s.is_dead (mitigates upstream Forge-Neo #694)",
        cls.__qualname__,
    )
    return True


def is_applied(cls=None) -> bool:
    """Return True if :func:`apply` has installed the wrapper on ``cls``."""
    if cls is None:
        cls = _resolve_default_class()
    if cls is None:
        return False
    return bool(getattr(cls, _PATCHED_FLAG, False))


def register_capabilities() -> None:
    """Register the ``memmgmt/issue-694-guard`` capability.

    Predicate fires when the patch is currently installed on the
    upstream ``LoadedModel`` class.
    """
    from modules.cplugapi import capabilities

    capabilities.register(
        "memmgmt/issue-694-guard",
        predicate=lambda: is_applied(None),
    )
