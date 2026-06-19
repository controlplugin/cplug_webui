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
import threading

_log = logging.getLogger(__name__)

_PATCHED_FLAG = "_cplugapi_isdead_patched"

# Flag stamped on ``modules.processing`` once the OOM-recovery wrapper is
# installed, so a webui reload / second call doesn't double-wrap.
_OOM_HOOK_FLAG = "_cplug_oom_recovery_hook_installed"
_oom_hook_lock = threading.Lock()


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


def install_oom_recovery_hook() -> bool:
    """Wrap ``modules.processing.process_images_inner`` so an OOM raised
    during generation reclaims VRAM before the error reaches the client.

    Why this exists (audit 02 — headless OOM recovery): upstream's
    auto-recovery only fires on the Gradio UI path (``modules/ui.py``
    cleanup wired via Gradio events). The ControlPlugin API path never
    hits that — ``/sdapi`` + ``/cplugapi`` generation calls
    ``process_images(p)`` directly under ``queue_lock`` in
    ``modules/api/api.py``, never ``main_thread.run_and_wait_result``.
    So on the API path an OOM currently leaves VRAM occupied and can
    wedge the next request. This wrapper restores headless recovery.

    Behaviour of the wrapper:

    - Runs the original ``process_images_inner`` in try/except.
    - On exception, lazily ``from backend import memory_management`` and
      ask ``memory_management.is_oom(e)`` whether it's an
      OOM/accelerator error. If so, lazily ``import modules.sd_models``
      and call ``sd_models.unload_model_weights()`` — that frees the
      base model AND drains the ControlNet cache (the unload hook in
      ``controlnet_cache.install_unload_hook`` wraps
      ``unload_model_weights`` to also call ``clear_cache()``). Then
      the original exception is **re-raised** so the request still
      surfaces a clean error and the NEXT request starts on freed VRAM.
    - Non-OOM exceptions are re-raised unchanged (no unload).
    - If ``backend.memory_management`` is not importable (stub env), the
      exception is re-raised without classification — never swallowed.

    Ordering: this must be the OUTERMOST of the ``process_images_inner``
    wrappers, so install it LAST (after ``gen_timing.install_hooks`` and
    ``auto_preempt.install_hooks``). That way the recovery sees the fully
    unwound generation stack — by the time the exception propagates here,
    the inner wrappers have already returned/raised through their own
    bookkeeping.

    Idempotent and fail-soft:

    Returns
    -------
    bool
        True when this call newly installed the wrapper. False when it
        was already installed, or ``modules.processing`` /
        ``process_images_inner`` is unavailable (logged, not raised).
    """
    with _oom_hook_lock:
        try:
            from modules import processing as _proc
        except ImportError:
            _log.warning(
                "cplugapi: modules.processing unavailable; "
                "OOM-recovery hook not installed"
            )
            return False

        if getattr(_proc, _OOM_HOOK_FLAG, False):
            return False

        original = getattr(_proc, "process_images_inner", None)
        if not callable(original):
            _log.warning(
                "cplugapi: process_images_inner missing/not callable; "
                "OOM-recovery hook not installed"
            )
            return False

        setattr(_proc, _OOM_HOOK_FLAG, True)

        def wrapped_process_images_inner(p, *args, **kwargs):
            try:
                return original(p, *args, **kwargs)
            except Exception as e:
                # Classify only when the backend is importable; in the
                # stub test env we can't tell OOM from anything else, so
                # we re-raise untouched rather than guess.
                try:
                    from backend import memory_management  # type: ignore
                except Exception:
                    raise
                if memory_management.is_oom(e):
                    _log.warning(
                        "cplugapi: OOM during generation — unloading model "
                        "weights to reclaim VRAM before re-raising"
                    )
                    try:
                        import modules.sd_models as _sd_models

                        _sd_models.unload_model_weights()
                    except Exception:
                        # Recovery is best-effort; never mask the OOM with
                        # a secondary error from the cleanup path.
                        _log.exception(
                            "cplugapi: OOM-recovery unload_model_weights failed"
                        )
                # Always re-raise the original error so the request gets a
                # clean failure and the next request starts fresh.
                raise

        wrapped_process_images_inner.__name__ = "process_images_inner"
        wrapped_process_images_inner.__qualname__ = "process_images_inner"
        _proc.process_images_inner = wrapped_process_images_inner
        _log.info(
            "cplugapi: installed headless OOM-recovery hook on "
            "process_images_inner"
        )
        return True


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
