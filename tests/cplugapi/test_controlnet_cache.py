"""Tests for ``modules.cplugapi.controlnet_cache``.

The module monkey-patches ``backend.patcher.controlnet.apply_controlnet_advanced``
to cache the patched UNet clone across gens. We test against synthetic
stand-ins for the upstream functions so the suite doesn't drag torch
into the test environment — same pattern as
``test_memmgmt_patches.py``.

Coverage:
- Cache miss patches fresh; cache hit reuses the cloned UNet
- Per-gen state (cond_hint, strength, weighting fields) is mutated on hit
- Different (unet, controlnet) baseline pairs do NOT collide
- Stale entries (one baseline GC'd) don't serve cached results
- Env-var kill switch (``CPLUG_CONTROLNET_CACHE=0``) restores passthrough
- Idempotent install
- Capability registered when enabled
"""

from __future__ import annotations

import gc
import logging
import sys
import types

import pytest


@pytest.fixture
def fake_controlnet_module(monkeypatch):
    """Build a stub ``backend.patcher.controlnet`` exposing
    ``apply_controlnet_advanced`` and the per-call objects (Unet,
    ControlNet, cnet) the wrapper expects.

    The stub functions track call counts so tests can prove the
    cache is hitting (or missing) without inspecting cache state.
    """

    module = types.ModuleType("backend.patcher.controlnet")
    counters = {"original_calls": 0}

    class _Cnet:
        """Stand-in for the ControlNet patcher clone created inside
        ``apply_controlnet_advanced``. Tracks the per-gen state so
        tests can verify mutate-on-hit."""

        def __init__(self, baseline):
            self.baseline = baseline
            self.cond_hint_original = None
            self.strength = None
            self.timestep_percent_range = None
            self.control_type_value = None
            self.positive_advanced_weighting = None
            self.negative_advanced_weighting = None
            self.advanced_frame_weighting = None
            self.advanced_sigma_weighting = None
            self.advanced_mask_weighting = None
            # Linked-list field on a real ControlNet — we don't use it
            # in tests but a few wrappers iterate ``previous_controlnet``.
            self.previous_controlnet = None

        def set_cond_hint(self, cond_hint, strength=1.0, timestep_percent_range=(0.0, 1.0)):
            self.cond_hint_original = cond_hint
            self.strength = strength
            self.timestep_percent_range = timestep_percent_range
            return self

        def set_control_type(self, control_type):
            self.control_type_value = control_type
            return self

    class _Unet:
        """Stand-in for the UNet patcher clone returned by
        ``apply_controlnet_advanced``. Holds the cnet via
        ``controlnet_linked_list`` to mirror the upstream structure."""

        def __init__(self):
            self.controlnet_linked_list = None

    def original(unet, controlnet, image_bchw, strength,
                 start_percent, end_percent,
                 positive_advanced_weighting=None,
                 negative_advanced_weighting=None,
                 advanced_frame_weighting=None,
                 advanced_sigma_weighting=None,
                 advanced_mask_weighting=None,
                 control_type=None):
        counters["original_calls"] += 1
        cnet = _Cnet(baseline=controlnet)
        cnet.set_cond_hint(image_bchw, strength, (start_percent, end_percent))
        cnet.set_control_type(control_type)
        cnet.positive_advanced_weighting = positive_advanced_weighting
        cnet.negative_advanced_weighting = negative_advanced_weighting
        cnet.advanced_frame_weighting = advanced_frame_weighting
        cnet.advanced_sigma_weighting = advanced_sigma_weighting
        cnet.advanced_mask_weighting = advanced_mask_weighting
        m = _Unet()
        m.controlnet_linked_list = cnet
        return m

    module.apply_controlnet_advanced = original
    monkeypatch.setitem(sys.modules, "backend", types.ModuleType("backend"))
    monkeypatch.setitem(sys.modules, "backend.patcher", types.ModuleType("backend.patcher"))
    monkeypatch.setitem(sys.modules, "backend.patcher.controlnet", module)

    return module, counters, _Cnet, _Unet


@pytest.fixture
def fresh_cache():
    """Reset cache + install state before/after each test."""
    from modules.cplugapi import controlnet_cache

    controlnet_cache.reset_cache_for_tests()
    yield controlnet_cache
    controlnet_cache.reset_cache_for_tests()


class _Baseline:
    """Plain class (not SimpleNamespace) so weakref.ref() works.

    types.SimpleNamespace lacks a ``__weakref__`` slot in its default
    layout, so the cache's weakref guard would treat every entry as
    immediately stale. Real Forge ``UnetPatcher`` / ``ControlNet``
    classes are regular Python classes with full weakref support, so
    this fixture more accurately mirrors production.
    """

    def __init__(self, name):
        self.name = name


def _baseline_pair():
    """Return a (baseline_unet, baseline_controlnet) pair that survives
    GC for the test's lifetime. The cache uses ``id(...)`` keys plus
    weakrefs — both args must outlive the test scope."""
    return _Baseline("unet_baseline"), _Baseline("cn_baseline")


def test_first_call_misses_and_invokes_original(
    fake_controlnet_module, fresh_cache,
):
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn = _baseline_pair()
    module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 1.0)

    assert counters["original_calls"] == 1
    assert fresh_cache.cache_size() == 1


def test_second_call_hits_cache_and_reuses_unet(
    fake_controlnet_module, fresh_cache,
):
    """The cached UNet patcher must be returned by-identity on hit so
    Forge's ``current_loaded_models.index(...)`` lookup succeeds and
    the clone-cleanup path is skipped."""
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn = _baseline_pair()
    m1 = module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 1.0)
    m2 = module.apply_controlnet_advanced(unet, cn, "img2", 0.7, 0.0, 1.0)

    assert m1 is m2  # identity preserved across gens — the whole point
    assert counters["original_calls"] == 1  # original ran once total


def test_cache_hit_mutates_per_gen_state(
    fake_controlnet_module, fresh_cache,
):
    """The cnet attached to the cached UNet must reflect THIS gen's
    inputs — not the previous gen's. If the cache served the old cnet
    unchanged, diffusion would use stale conditioning."""
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn = _baseline_pair()
    module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 0.8, control_type="depth")
    m = module.apply_controlnet_advanced(unet, cn, "img2", 0.9, 0.1, 1.0, control_type="canny")

    cnet = m.controlnet_linked_list
    assert cnet.cond_hint_original == "img2"
    assert cnet.strength == 0.9
    assert cnet.timestep_percent_range == (0.1, 1.0)
    assert cnet.control_type_value == "canny"


def test_cache_hit_mutates_advanced_weighting_fields(
    fake_controlnet_module, fresh_cache,
):
    """The five ``advanced_*_weighting`` attrs must also be re-set on
    every call. Otherwise a gen that used a custom weighting would
    leak that state into the next gen which expected defaults."""
    module, _, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn = _baseline_pair()
    custom = {"input": [0.5] * 12, "middle": [1.0], "output": [0.5] * 12}
    module.apply_controlnet_advanced(
        unet, cn, "img1", 0.5, 0.0, 1.0,
        positive_advanced_weighting=custom,
    )
    m = module.apply_controlnet_advanced(
        unet, cn, "img2", 0.5, 0.0, 1.0,
        positive_advanced_weighting=None,  # back to default
    )
    cnet = m.controlnet_linked_list
    assert cnet.positive_advanced_weighting is None  # NOT the stale custom


def test_different_baselines_do_not_collide(
    fake_controlnet_module, fresh_cache,
):
    """A different (unet, controlnet) pair must not return the cached
    entry from another pair. Without this, swapping ControlNet model
    between gens would silently keep using the old one."""
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet1, cn1 = _baseline_pair()
    unet2, cn2 = _baseline_pair()

    m1 = module.apply_controlnet_advanced(unet1, cn1, "img", 0.5, 0.0, 1.0)
    m2 = module.apply_controlnet_advanced(unet2, cn2, "img", 0.5, 0.0, 1.0)
    m3 = module.apply_controlnet_advanced(unet1, cn2, "img", 0.5, 0.0, 1.0)

    assert m1 is not m2  # different baselines
    assert m1 is not m3  # mixed baselines
    assert m2 is not m3
    assert counters["original_calls"] == 3
    assert fresh_cache.cache_size() == 3


def test_stale_entry_is_dropped_when_baseline_gc(
    fake_controlnet_module, fresh_cache,
):
    """When a baseline is GC'd, ``id()`` may be recycled for a different
    object. The weakref guard must reject the stale entry instead of
    serving a wrong cached result."""
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    # Create a baseline, populate the cache, then drop the reference.
    unet1, cn1 = _baseline_pair()
    module.apply_controlnet_advanced(unet1, cn1, "img1", 0.5, 0.0, 1.0)
    assert counters["original_calls"] == 1

    # Capture the stale ids so we can verify behaviour even if Python
    # reuses them. We can't force id reuse deterministically, but the
    # weakref check makes the test reliable: once the original objects
    # are dead, ``entry.is_alive(...)`` returns False regardless of
    # whether the new objects landed at the recycled id.
    del unet1, cn1
    gc.collect()

    # New baselines — may or may not get the same ids, but the weakrefs
    # to the previous objects are now dead either way.
    unet2, cn2 = _baseline_pair()
    module.apply_controlnet_advanced(unet2, cn2, "img2", 0.5, 0.0, 1.0)

    # The original ran a SECOND time (cache rejected the stale entry
    # OR the id keys differed — either way we want a fresh patch).
    assert counters["original_calls"] == 2


def test_kill_switch_disables_cache(
    monkeypatch, fake_controlnet_module, fresh_cache,
):
    """``CPLUG_CONTROLNET_CACHE=0`` must restore upstream behaviour:
    every call hits the original, no cache is populated. Read-once-at-
    install means we must apply AFTER setting the env var."""
    monkeypatch.setenv("CPLUG_CONTROLNET_CACHE", "0")
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn = _baseline_pair()
    module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 1.0)
    module.apply_controlnet_advanced(unet, cn, "img2", 0.7, 0.0, 1.0)

    assert counters["original_calls"] == 2
    assert fresh_cache.cache_size() == 0


def test_install_is_idempotent(fake_controlnet_module, fresh_cache):
    """Repeated apply() calls must not double-wrap. Otherwise the cache
    layer would compose with itself and the original-call counter would
    decouple from real caller intent."""
    module, counters, _, _ = fake_controlnet_module
    assert fresh_cache.apply(module) is True
    assert fresh_cache.apply(module) is False  # already installed
    assert fresh_cache.apply(module) is False

    unet, cn = _baseline_pair()
    module.apply_controlnet_advanced(unet, cn, "img", 0.5, 0.0, 1.0)
    module.apply_controlnet_advanced(unet, cn, "img", 0.5, 0.0, 1.0)

    assert counters["original_calls"] == 1


def test_apply_with_missing_target_returns_false(fresh_cache):
    """If ``backend.patcher.controlnet`` isn't importable, apply() must
    be a silent no-op rather than crashing the cplugapi mount."""
    # Don't install the fake module — _resolve_target_module() will
    # fail to import.
    assert fresh_cache.apply(target_module=None) is False


def test_apply_logs_warning_when_symbol_missing(
    fake_controlnet_module, fresh_cache, caplog,
):
    """If upstream renames apply_controlnet_advanced, the operator
    needs to know on the next webui boot rather than silently losing
    the perf optimisation."""
    module, _, _, _ = fake_controlnet_module
    delattr(module, "apply_controlnet_advanced")

    logger = logging.getLogger("modules.cplugapi.controlnet_cache")
    original_propagate = logger.propagate
    logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            assert fresh_cache.apply(module) is False
    finally:
        logger.propagate = original_propagate

    assert any(
        "no apply_controlnet_advanced" in r.getMessage()
        for r in caplog.records if r.name == logger.name
    )


def test_capability_registered_when_enabled(fresh_cache, clean_capabilities):
    """``controlnet/patcher-cache`` advertises that the optimisation is
    live. Clients can use it to detect whether the fork is the kind that
    avoids the per-gen detach/reattach tax."""
    fresh_cache.register_capabilities()
    assert "controlnet/patcher-cache" in clean_capabilities.enabled_capabilities()


def test_capability_omitted_when_disabled(
    monkeypatch, fresh_cache, clean_capabilities,
):
    monkeypatch.setenv("CPLUG_CONTROLNET_CACHE", "0")
    fresh_cache.register_capabilities()
    assert "controlnet/patcher-cache" not in clean_capabilities.enabled_capabilities()
