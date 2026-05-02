"""Swap-stress regression for upstream Forge-Neo #694 (audit 02 Phase C).

Simulates 20 checkpoint swaps against the patched ``is_dead`` wrapper
and asserts:

1. No exception escapes the patched method, even when ``real_model``
   has been reassigned to ``None`` mid-cycle (the precondition that
   triggers the upstream crash).
2. The fake "registry" stays bounded — i.e. the swap loop doesn't
   leak entries that the dead-slot check failed to flag for cleanup.
3. The whole thing runs in well under 2 seconds, matching the
   spec's latency budget for cplugapi unit tests.
"""

from __future__ import annotations

import time
import weakref

from modules.cplugapi import memmgmt_patches


class _Weakable:
    """Stand-in for the torch ``nn.Module`` upstream stores a weakref to.

    Bare ``object()`` instances are not weakref-able, so we use a
    small custom class instead.
    """


class _FakeLoaded:
    """Minimal stand-in for ``backend.memory_management.LoadedModel``."""

    def __init__(self, real, model):
        if real is None:
            self.real_model = None
        else:
            self._real = real
            self.real_model = weakref.ref(real)
        self.model = model

    def is_dead(self) -> bool:
        return self.real_model() is not None and self.model is None


def _cleanup(registry: list[_FakeLoaded]) -> None:
    """Mimic ``free_memory`` / ``cleanup_models``: drop dead slots."""
    registry[:] = [m for m in registry if not m.is_dead()]


def test_swap_stress_no_exceptions_and_bounded():
    memmgmt_patches.apply(_FakeLoaded)

    registry: list[_FakeLoaded] = []
    started = time.perf_counter()

    # 20 simulated swaps: each cycle loads a "checkpoint", then
    # unloads it (real_model -> None). The patched is_dead must
    # tolerate the second state and let cleanup remove the entry.
    for i in range(20):
        real = _Weakable()
        slot = _FakeLoaded(real=real, model=object())
        registry.append(slot)
        # Pre-cleanup: a freshly-loaded slot is alive.
        assert slot.is_dead() is False
        # Simulate model_unload: real_model -> None.
        slot.real_model = None
        # is_dead must NOT raise here (this is the original bug).
        assert slot.is_dead() is True
        _cleanup(registry)
        # Registry should have shrunk back to empty after the dead
        # slot was filtered out.
        assert registry == []

    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"swap stress took {elapsed:.3f}s — expected <2s"


def test_swap_stress_mixed_alive_and_dead_slots():
    """Run a swap loop with overlapping alive/dead slots to make sure
    the patched cleanup leaves alive ones intact while reaping dead
    ones."""
    memmgmt_patches.apply(_FakeLoaded)

    registry: list[_FakeLoaded] = []
    real_alive_refs = []  # keep alive refs out of band so weakrefs survive

    for _ in range(20):
        # One alive slot, one slot we immediately kill.
        alive_real = _Weakable()
        real_alive_refs.append(alive_real)
        alive = _FakeLoaded(real=alive_real, model=object())
        dying_real = _Weakable()
        dying = _FakeLoaded(real=dying_real, model=object())
        dying.real_model = None  # immediately dead
        registry.extend([alive, dying])

        _cleanup(registry)

    # Every loop added one alive + one dead; only the alive ones
    # should remain.
    assert len(registry) == 20
    assert all(not m.is_dead() for m in registry)
