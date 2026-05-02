"""Unit tests for ``modules.cplugapi.memmgmt_patches`` (audit 02 Phase C).

We exercise the wrapper via a hand-rolled fake ``LoadedModel``-shaped
class. That keeps the test independent of ``backend/memory_management``
(which would drag in torch + the global args parser) while still
covering every branch of the patched ``is_dead`` logic.
"""

from __future__ import annotations

import weakref


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
