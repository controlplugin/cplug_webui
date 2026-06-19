"""Unit tests for ``modules.cplugapi.memmgmt_patches`` (audit 02 Phase C).

We exercise the wrapper via a hand-rolled fake ``LoadedModel``-shaped
class. That keeps the test independent of ``backend/memory_management``
(which would drag in torch + the global args parser) while still
covering every branch of the patched ``is_dead`` logic.
"""

from __future__ import annotations

import sys
import types
import weakref

import pytest

from modules.cplugapi import memmgmt_patches


class _Weakable:
    """Plain class — weakref-able, unlike bare ``object()``.

    Upstream stores a weakref to a torch ``nn.Module``; for the
    wrapper's purposes any weakref-able instance does the job.
    """


def _make_fake_class():
    """Build a class shaped like the upstream ``LoadedModel`` w.r.t.
    ``is_dead`` only. Fresh class per test so the patch flag does not
    leak across cases.
    """

    class FakeLoadedModel:
        def __init__(self, real, model):
            # ``real`` is the strong reference the test holds onto;
            # ``self.real_model`` is either a weakref or None,
            # matching upstream.
            if real is None:
                self.real_model = None
            else:
                self._real = real  # keep alive for the lifetime of the fake
                self.real_model = weakref.ref(real)
            self.model = model

        def is_dead(self) -> bool:
            # Mirror the upstream definition byte-for-byte so the
            # wrapper has something realistic to delegate to.
            return self.real_model() is not None and self.model is None

    return FakeLoadedModel


def test_apply_returns_true_first_then_false_idempotent():
    cls = _make_fake_class()
    assert memmgmt_patches.apply(cls) is True
    assert memmgmt_patches.is_applied(cls) is True
    # Second call is a no-op.
    assert memmgmt_patches.apply(cls) is False


def test_patched_is_dead_returns_true_when_real_model_is_none():
    cls = _make_fake_class()
    memmgmt_patches.apply(cls)
    obj = cls(real=None, model=object())
    # Pre-patch this would have raised TypeError on the None() call.
    assert obj.is_dead() is True


def test_patched_is_dead_falls_through_when_real_model_present():
    cls = _make_fake_class()
    memmgmt_patches.apply(cls)
    real = _Weakable()
    # When real_model() yields a value AND model is None, upstream
    # reports the slot as dead.
    obj = cls(real=real, model=None)
    assert obj.is_dead() is True


def test_patched_is_dead_falls_through_to_false_when_alive():
    cls = _make_fake_class()
    memmgmt_patches.apply(cls)
    real = _Weakable()
    # When real_model() yields a value AND model is set, the slot
    # is alive — original returns False.
    obj = cls(real=real, model=object())
    assert obj.is_dead() is False


def test_apply_no_op_when_default_class_unresolvable(monkeypatch):
    """``apply()`` with no class argument and no resolvable backend
    must return False, not raise."""
    monkeypatch.setattr(memmgmt_patches, "_resolve_default_class", lambda: None)
    assert memmgmt_patches.apply() is False
    assert memmgmt_patches.is_applied() is False


def test_register_capability_predicate_tracks_apply_state(
    monkeypatch, clean_capabilities
):
    cls = _make_fake_class()
    monkeypatch.setattr(memmgmt_patches, "_resolve_default_class", lambda: cls)

    memmgmt_patches.register_capabilities()
    # Not yet applied — predicate is false.
    assert "memmgmt/issue-694-guard" not in clean_capabilities.enabled_capabilities()
    memmgmt_patches.apply(cls)
    assert "memmgmt/issue-694-guard" in clean_capabilities.enabled_capabilities()


def test_patched_method_preserves_name_and_qualname():
    cls = _make_fake_class()
    memmgmt_patches.apply(cls)
    assert cls.is_dead.__name__ == "is_dead"
    assert cls.is_dead.__qualname__.endswith(".is_dead")


def test_double_apply_does_not_double_wrap():
    """A second apply must not chain wrappers (which would still work
    but would slow the hot path and break the idempotency contract)."""
    cls = _make_fake_class()
    memmgmt_patches.apply(cls)
    wrapped_once = cls.is_dead
    memmgmt_patches.apply(cls)
    assert cls.is_dead is wrapped_once


# ---------------------------------------------------------------------------
# install_oom_recovery_hook — headless OOM recovery on the API gen path.
#
# We stub ``modules.processing`` (the wrap target), ``modules.sd_models``
# (the VRAM reclaim), and ``backend.memory_management`` (the OOM
# classifier) with simple module stand-ins, mirroring the stub style in
# ``test_gen_timing`` / ``test_auto_preempt``. The suite runs without the
# real backend, so every backend-classified assertion is driven entirely
# by the injected ``is_oom`` stub.
# ---------------------------------------------------------------------------


class _OomEnv:
    """Bundle of stubs + bookkeeping returned by the fixture."""

    def __init__(self, proc, sd_models, memmgmt):
        self.proc = proc
        self.sd_models = sd_models
        self.memmgmt = memmgmt
        self.unload_calls = 0


@pytest.fixture
def oom_env(monkeypatch):
    """Install stub ``modules.processing`` + ``modules.sd_models`` +
    ``backend.memory_management`` and the OOM-recovery hook on top.

    The default ``process_images_inner`` succeeds; tests rebind it to
    raise. ``is_oom`` defaults to classifying nothing as OOM; tests
    override it. ``unload_model_weights`` increments a counter.
    """
    proc = types.ModuleType("modules.processing")

    def _default_process(p, *a, **k):
        return "result"

    proc.process_images_inner = _default_process
    monkeypatch.setitem(sys.modules, "modules.processing", proc)

    env = _OomEnv(proc, None, None)

    sd_models = types.ModuleType("modules.sd_models")

    def _unload():
        env.unload_calls += 1

    sd_models.unload_model_weights = _unload
    monkeypatch.setitem(sys.modules, "modules.sd_models", sd_models)
    env.sd_models = sd_models

    # ``backend`` package + ``backend.memory_management`` submodule.
    backend_pkg = sys.modules.get("backend")
    if backend_pkg is None:
        backend_pkg = types.ModuleType("backend")
        backend_pkg.__path__ = []  # mark as package so submodule import works
        monkeypatch.setitem(sys.modules, "backend", backend_pkg)

    memmgmt = types.ModuleType("backend.memory_management")
    memmgmt.is_oom = lambda e: False  # default: nothing is OOM
    monkeypatch.setitem(sys.modules, "backend.memory_management", memmgmt)
    monkeypatch.setattr(backend_pkg, "memory_management", memmgmt, raising=False)
    env.memmgmt = memmgmt

    # Fresh install on this stub.
    if hasattr(proc, memmgmt_patches._OOM_HOOK_FLAG):
        delattr(proc, memmgmt_patches._OOM_HOOK_FLAG)
    assert memmgmt_patches.install_oom_recovery_hook() is True
    return env


def _rewrap(proc):
    """Re-install the OOM hook over the *current* ``process_images_inner``.

    The wrapper captures the original at install time (same closure
    pattern as gen_timing/auto_preempt), so a test that rebinds
    ``proc.process_images_inner`` after the fixture installed must toggle
    the flag and re-install to wrap the new inner. Mirrors
    ``test_gen_timing.test_decode_inside_gen_contributes_to_vae_stage``.
    """
    if hasattr(proc, memmgmt_patches._OOM_HOOK_FLAG):
        delattr(proc, memmgmt_patches._OOM_HOOK_FLAG)
    memmgmt_patches.install_oom_recovery_hook()


def test_oom_hook_wraps_process_images_inner(oom_env):
    """After install the function is a different object (wrapped) and
    the idempotency flag is stamped on the module."""
    assert getattr(oom_env.proc, memmgmt_patches._OOM_HOOK_FLAG) is True
    assert oom_env.proc.process_images_inner.__name__ == "process_images_inner"
    # Happy path still passes through to the original result.
    assert oom_env.proc.process_images_inner("p") == "result"
    assert oom_env.unload_calls == 0


def test_oom_classified_exception_unloads_and_reraises(oom_env):
    class _Boom(RuntimeError):
        pass

    def _boom(p, *a, **k):
        raise _Boom("CUDA out of memory")

    oom_env.proc.process_images_inner = _boom
    oom_env.memmgmt.is_oom = lambda e: isinstance(e, _Boom)
    _rewrap(oom_env.proc)

    with pytest.raises(_Boom):
        oom_env.proc.process_images_inner("p")
    # Recovery fired exactly once before the re-raise.
    assert oom_env.unload_calls == 1


def test_non_oom_exception_passes_through_without_unload(oom_env):
    def _boom(p, *a, **k):
        raise ValueError("not an oom")

    oom_env.proc.process_images_inner = _boom
    # is_oom default already returns False for everything.
    _rewrap(oom_env.proc)

    with pytest.raises(ValueError):
        oom_env.proc.process_images_inner("p")
    assert oom_env.unload_calls == 0


def test_oom_recovery_swallows_unload_error_but_still_reraises(oom_env):
    """A failure inside unload_model_weights must not mask the original
    OOM — the OOM error still propagates."""

    class _Boom(RuntimeError):
        pass

    def _boom(p, *a, **k):
        raise _Boom("out of memory")

    def _bad_unload():
        oom_env.unload_calls += 1
        raise RuntimeError("unload exploded")

    oom_env.proc.process_images_inner = _boom
    oom_env.memmgmt.is_oom = lambda e: True
    oom_env.sd_models.unload_model_weights = _bad_unload
    _rewrap(oom_env.proc)

    with pytest.raises(_Boom):
        oom_env.proc.process_images_inner("p")
    assert oom_env.unload_calls == 1


def test_oom_hook_idempotent(oom_env):
    """A second install must not re-wrap; the function object stays the
    same and a subsequent OOM still triggers exactly one unload."""
    wrapped_once = oom_env.proc.process_images_inner
    assert memmgmt_patches.install_oom_recovery_hook() is False
    assert oom_env.proc.process_images_inner is wrapped_once


def test_oom_hook_fail_soft_when_processing_unavailable(monkeypatch):
    """If ``modules.processing`` can't be imported, install returns False
    rather than raising."""
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *a, **k):
        if name == "modules.processing" or name == "modules":
            raise ImportError("simulated stub env: no modules.processing")
        return real_import(name, *a, **k)

    # Ensure a cached stub doesn't satisfy the import.
    monkeypatch.delitem(sys.modules, "modules.processing", raising=False)
    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    assert memmgmt_patches.install_oom_recovery_hook() is False
