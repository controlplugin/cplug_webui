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


class _ControlNetWrapper:
    """Stand-in for Forge's ``ControlNet`` patcher (NOT the cldm one).

    Forge rebuilds this wrapper every gen via ``try_build_from_state_dict``,
    but the inner ``control_model`` (cldm.ControlNet weights) is cached
    in ``_CONTROL_MODEL_CACHE``. The cache key strategy walks to
    ``control_model`` for stability — this fixture lets us exercise that
    path."""

    def __init__(self, control_model):
        self.control_model = control_model


def _baseline_pair():
    """Return a (baseline_unet, baseline_controlnet) pair that survives
    GC for the test's lifetime. The cache uses ``id(...)`` keys plus
    weakrefs — both args must outlive the test scope.

    The controlnet baseline is a plain ``_Baseline`` object (no inner
    ``control_model`` field) so the cache falls back to keying on the
    wrapper itself — covers the unknown-subclass codepath. Tests that
    exercise the production wrapper-vs-weights split use
    ``_baseline_pair_with_inner_weights``.
    """
    return _Baseline("unet_baseline"), _Baseline("cn_baseline")


def _baseline_pair_with_inner_weights(shared_weights=None):
    """Return (unet, ControlNet-wrapper-with-shared-inner-weights).

    Mirrors the production bug: Forge's ``try_load_supported_control_model``
    returns a fresh wrapper every gen but the inner cldm weights come
    from ``_CONTROL_MODEL_CACHE``. The cache key must stabilize on the
    inner weights so back-to-back gens hit even though the wrapper id
    differs.

    Pass an existing ``shared_weights`` to simulate two gens sharing
    the same inner cldm.ControlNet (the production happy path); pass
    ``None`` to allocate a fresh one.
    """
    unet = _Baseline("unet_baseline")
    if shared_weights is None:
        shared_weights = _Baseline("cn_inner_weights")
    return unet, _ControlNetWrapper(shared_weights), shared_weights


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


# --- regression: cache key stability across rebuilt wrappers --------------
#
# Real-world bug observed on the test rig: Forge's controlnet integration
# (``extensions-builtin/sd_forge_controlnet/scripts/controlnet.py:378``)
# calls ``try_load_supported_control_model`` every gen, returning a FRESH
# ``ControlNet`` wrapper. The underlying cldm weights are cached in
# ``_CONTROL_MODEL_CACHE``, but the wrapper isn't. Naive ``id(controlnet)``
# keying misses every gen → cache never hits in production. The fix walks
# to the inner ``control_model`` for the stable identity.


def test_fresh_wrapper_around_shared_weights_hits_cache(
    fake_controlnet_module, fresh_cache,
):
    """The production happy path: two consecutive gens see two different
    ControlNet wrapper instances backing the same inner weights. Cache
    must hit on the second gen — that's the entire reason we walk to
    ``control_model`` for the key."""
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn_gen1, shared_weights = _baseline_pair_with_inner_weights()
    m1 = module.apply_controlnet_advanced(unet, cn_gen1, "img1", 0.5, 0.0, 1.0)

    # Second "gen": Forge rebuilt the wrapper but reused the cached
    # cldm weights. Different id(controlnet), same id(control_model).
    _, cn_gen2, _ = _baseline_pair_with_inner_weights(shared_weights=shared_weights)
    assert cn_gen1 is not cn_gen2
    assert cn_gen1.control_model is cn_gen2.control_model

    m2 = module.apply_controlnet_advanced(unet, cn_gen2, "img2", 0.7, 0.0, 1.0)

    assert m1 is m2  # cache hit — same patched UNet returned
    assert counters["original_calls"] == 1


def test_different_inner_weights_do_not_collide(
    fake_controlnet_module, fresh_cache,
):
    """Inverse of the above: same wrapper class, DIFFERENT inner weights
    (e.g., user swapped the ControlNet checkpoint mid-session). Must NOT
    hit cache."""
    module, counters, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn_a, _ = _baseline_pair_with_inner_weights()
    unet2, cn_b, _ = _baseline_pair_with_inner_weights()  # different inner

    m1 = module.apply_controlnet_advanced(unet, cn_a, "img", 0.5, 0.0, 1.0)
    m2 = module.apply_controlnet_advanced(unet, cn_b, "img", 0.5, 0.0, 1.0)

    assert m1 is not m2
    assert counters["original_calls"] == 2


def test_resolves_control_weights_field_for_controllora():
    """ControlLora subclass exposes ``control_weights`` instead of
    ``control_model`` — the resolver must walk to it."""
    from modules.cplugapi.controlnet_cache import _resolve_controlnet_inner

    weights = _Baseline("controllora_weights")
    wrapper = types.SimpleNamespace(control_weights=weights)
    assert _resolve_controlnet_inner(wrapper) is weights


def test_resolves_t2i_model_field_for_t2iadapter():
    """T2IAdapter subclass exposes ``t2i_model``."""
    from modules.cplugapi.controlnet_cache import _resolve_controlnet_inner

    weights = _Baseline("t2i_weights")
    wrapper = types.SimpleNamespace(t2i_model=weights)
    assert _resolve_controlnet_inner(wrapper) is weights


def test_resolves_falls_back_to_wrapper_for_unknown_subclass():
    """Unknown subclass with no recognised field — resolver returns the
    wrapper itself. The cache will then key on it (same as pre-fix
    behaviour) which means cache effectively disabled for that
    subclass — the conservative correct outcome."""
    from modules.cplugapi.controlnet_cache import _resolve_controlnet_inner

    wrapper = types.SimpleNamespace(some_other_field="x")
    assert _resolve_controlnet_inner(wrapper) is wrapper


# --- regression: from-import consumer rebinding ---------------------------
#
# Production bug discovered after the inner-weights fix shipped:
# ``modules_forge/supported_controlnet.py`` does ``from backend.patcher.
# controlnet import apply_controlnet_advanced`` at import time, capturing
# the ORIGINAL function. Our monkey-patch on the source module didn't
# update that local copy, so the consumer kept calling the unwrapped
# function and the cache silently never fired. The install path now
# walks ``_KNOWN_CONSUMER_MODULES`` and rebinds each consumer's local
# symbol if it still points at the original.


def test_install_rebinds_known_consumer_module(
    fake_controlnet_module, fresh_cache, monkeypatch,
):
    """The install path must rebind ``modules_forge.supported_controlnet.
    apply_controlnet_advanced`` so the consumer's call site actually
    routes through our wrapper. Otherwise the cache is dead code in
    production."""
    module, _, _, _ = fake_controlnet_module
    original = module.apply_controlnet_advanced

    # Simulate a consumer that did ``from backend.patcher.controlnet
    # import apply_controlnet_advanced`` — captures the original.
    consumer = types.ModuleType("modules_forge.supported_controlnet")
    consumer.apply_controlnet_advanced = original
    monkeypatch.setitem(sys.modules, "modules_forge.supported_controlnet", consumer)

    fresh_cache.apply(module)

    # After install, the consumer's binding must point at the wrapper,
    # not the original — otherwise calls through the consumer still
    # bypass the cache.
    assert consumer.apply_controlnet_advanced is module.apply_controlnet_advanced
    assert consumer.apply_controlnet_advanced is not original


def test_install_does_not_clobber_consumer_with_custom_binding(
    fake_controlnet_module, fresh_cache, monkeypatch, caplog,
):
    """If a consumer module has rebound the symbol to its OWN function
    (some competing extension), don't clobber. Log a warning so the
    missed coverage is visible."""
    module, _, _, _ = fake_controlnet_module

    competing = lambda *args, **kwargs: "competing"  # noqa: E731
    consumer = types.ModuleType("modules_forge.supported_controlnet")
    consumer.apply_controlnet_advanced = competing
    monkeypatch.setitem(sys.modules, "modules_forge.supported_controlnet", consumer)

    logger = logging.getLogger("modules.cplugapi.controlnet_cache")
    original_propagate = logger.propagate
    logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            fresh_cache.apply(module)
    finally:
        logger.propagate = original_propagate

    assert consumer.apply_controlnet_advanced is competing  # untouched
    assert any(
        "is not the upstream function" in r.getMessage()
        for r in caplog.records if r.name == logger.name
    )


def test_install_silent_when_consumer_unloaded(
    fake_controlnet_module, fresh_cache, monkeypatch,
):
    """If the consumer module isn't in ``sys.modules`` yet, install is
    silent — the consumer's later import will pick up our wrapped
    function from the source module."""
    module, _, _, _ = fake_controlnet_module

    # Ensure the consumer key is NOT in sys.modules.
    monkeypatch.delitem(sys.modules, "modules_forge.supported_controlnet", raising=False)

    # Should not raise.
    assert fresh_cache.apply(module) is True


# --- ControlNet.cleanup unload-skip tests ---------------------------------
#
# Forge's ControlNet.cleanup() at end-of-gen calls
# memory_management.unload_model on the cnet's control_model_wrapped,
# popping it from current_loaded_models. The next gen's load_models_gpu
# lookup misses → "Requested to load ControlNet" log fires. For cnets
# we hold in cache, that unload is wasted work. The wrapped cleanup
# skips it for tagged cnets only.


def test_cnet_tagged_when_cached(fake_controlnet_module, fresh_cache):
    """Successful cache install must tag the cnet with
    ``_cplug_cache_held = True`` so the cleanup wrapper recognises it."""
    module, _, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn, _ = _baseline_pair_with_inner_weights()
    m = module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 1.0)

    assert getattr(m.controlnet_linked_list, "_cplug_cache_held", False) is True


def test_install_patches_controlnet_cleanup(fake_controlnet_module, fresh_cache):
    """The install path must wrap ``ControlNet.cleanup`` so cached cnets
    skip the unload_model call. Idempotent via the class-level flag."""
    module, _, _, _ = fake_controlnet_module

    class _StubControlNet:
        """Stand-in for ``backend.patcher.controlnet.ControlNet``. The
        original ``cleanup`` here is a sentinel we can identify after
        wrapping."""

        def cleanup(self):
            return "original_cleanup"

    module.ControlNet = _StubControlNet
    original_cleanup = _StubControlNet.cleanup

    fresh_cache.apply(module)

    assert _StubControlNet.cleanup is not original_cleanup
    assert getattr(_StubControlNet, "_cplugapi_controlnet_cleanup_patched", False) is True

    # Second apply on the same module — flag short-circuits, no double-wrap.
    second_cleanup = _StubControlNet.cleanup
    fresh_cache.apply(module)
    assert _StubControlNet.cleanup is second_cleanup


def test_wrapped_cleanup_skips_unload_for_tagged_cnet(monkeypatch):
    """Tagged cnet's cleanup must NOT invoke ``memory_management.unload_model``.
    Untagged cnet's cleanup invokes it normally."""
    from modules.cplugapi import controlnet_cache

    # Stub backend.memory_management with an unload_model we can spy on.
    fake_mm = types.ModuleType("backend.memory_management")
    unload_calls = []

    def fake_unload(model):
        unload_calls.append(model)
        return True

    fake_mm.unload_model = fake_unload
    monkeypatch.setitem(sys.modules, "backend", types.ModuleType("backend"))
    monkeypatch.setitem(sys.modules, "backend.memory_management", fake_mm)

    # Original cleanup that calls unload_model — mirroring real
    # ControlNet.cleanup behaviour.
    cleanup_calls = []

    def original_cleanup(self):
        cleanup_calls.append(("super", self))
        # Mirror real cleanup: unload then super-cleanup work.
        from backend import memory_management
        memory_management.unload_model(self.control_model_wrapped)

    wrapped = controlnet_cache._build_wrapped_cleanup(original_cleanup)

    class _Cnet:
        pass

    untagged = _Cnet()
    untagged.control_model_wrapped = "wrapped_a"
    wrapped(untagged)
    assert unload_calls == ["wrapped_a"]  # untagged: full cleanup ran

    tagged = _Cnet()
    tagged.control_model_wrapped = "wrapped_b"
    tagged._cplug_cache_held = True
    wrapped(tagged)
    # Tagged: the original cleanup ran, but the unload_model call inside
    # was short-circuited to a no-op. Original got called (cleanup_calls
    # grew) but unload_calls didn't grow.
    assert len(cleanup_calls) == 2
    assert unload_calls == ["wrapped_a"]  # unchanged from before


def test_wrapped_cleanup_restores_unload_after_call(monkeypatch):
    """The temporary swap of ``memory_management.unload_model`` must be
    restored after the cleanup call returns — concurrent cleanups (or
    later non-tagged cleanups) must see the real function."""
    from modules.cplugapi import controlnet_cache

    fake_mm = types.ModuleType("backend.memory_management")
    real_unload = lambda m: True  # noqa: E731
    fake_mm.unload_model = real_unload
    monkeypatch.setitem(sys.modules, "backend", types.ModuleType("backend"))
    monkeypatch.setitem(sys.modules, "backend.memory_management", fake_mm)

    def original_cleanup(self):
        pass

    wrapped = controlnet_cache._build_wrapped_cleanup(original_cleanup)

    class _Cnet:
        pass

    tagged = _Cnet()
    tagged._cplug_cache_held = True
    wrapped(tagged)

    # After the wrapped call, unload_model is back to the real function.
    assert fake_mm.unload_model is real_unload


# --- clear_cache + unload-hook tests --------------------------------------
#
# modules/sd_models.py:unload_model_weights() frees the base model on a
# checkpoint cycle but does NOT clear this cnet cache — the held patched
# UNet clone + cnet stay pinned until FIFO eviction. clear_cache() drops
# those strong refs and un-tags the held cnet so a later ControlNet.cleanup
# runs the REAL unload_model. install_unload_hook() wires clear_cache() to
# run right after the original unload_model_weights.


def test_clear_cache_empties_and_drops_refs(
    fake_controlnet_module, fresh_cache,
):
    """clear_cache() must empty the cache (cache_size()==0) and drop the
    strong ``cached_unet`` reference on every entry so the patched UNet
    clone is no longer pinned."""
    module, _, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn, _ = _baseline_pair_with_inner_weights()
    m = module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 1.0)
    assert fresh_cache.cache_size() == 1

    # Grab the live entry before clearing so we can assert its strong ref
    # is dropped (set to None) by clear_cache.
    with fresh_cache._cache_lock:
        entry = next(iter(fresh_cache._cache.values()))
    assert entry.cached_unet is m  # strong ref held pre-clear

    fresh_cache.clear_cache()

    assert fresh_cache.cache_size() == 0
    assert entry.cached_unet is None  # strong ref dropped


def test_clear_cache_untags_held_cnet(
    fake_controlnet_module, fresh_cache,
):
    """The held cnet (reachable via cached_unet.controlnet_linked_list)
    must have ``_cplug_cache_held`` cleared so a subsequent
    ControlNet.cleanup runs the REAL unload_model rather than the no-op."""
    module, _, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    unet, cn, _ = _baseline_pair_with_inner_weights()
    m = module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 1.0)
    cnet = m.controlnet_linked_list
    assert getattr(cnet, "_cplug_cache_held", False) is True  # tagged on install

    fresh_cache.clear_cache()

    assert getattr(cnet, "_cplug_cache_held", True) is False  # tag cleared


def test_clear_cache_safe_on_empty_cache(fresh_cache):
    """clear_cache() on an empty cache must be a no-op, not raise."""
    fresh_cache.reset_cache_for_tests()
    fresh_cache.clear_cache()
    assert fresh_cache.cache_size() == 0


def _install_fake_sd_models(monkeypatch):
    """Install a stub ``modules.sd_models`` exposing an
    ``unload_model_weights`` we can spy on. Returns (module, call_log)."""
    sd_models = types.ModuleType("modules.sd_models")
    calls = {"count": 0, "args": None, "kwargs": None}

    def unload_model_weights(*args, **kwargs):
        calls["count"] += 1
        calls["args"] = args
        calls["kwargs"] = kwargs
        return "freed"

    sd_models.unload_model_weights = unload_model_weights
    # ``import modules.sd_models`` needs ``modules`` to resolve too; the
    # top-level conftest already stubs parts of ``modules`` but we set the
    # attribute + sys.modules entry explicitly to be safe under any order.
    monkeypatch.setitem(sys.modules, "modules.sd_models", sd_models)
    return sd_models, calls


def test_install_unload_hook_wraps_and_clears_cache(
    fake_controlnet_module, fresh_cache, monkeypatch,
):
    """install_unload_hook must wrap unload_model_weights so the original
    runs (return value preserved) AND clear_cache fires afterward."""
    module, _, _, _ = fake_controlnet_module
    fresh_cache.apply(module)

    sd_models, calls = _install_fake_sd_models(monkeypatch)

    assert fresh_cache.install_unload_hook() is True

    # Populate the cache, then call the (now wrapped) unload.
    unet, cn, _ = _baseline_pair_with_inner_weights()
    module.apply_controlnet_advanced(unet, cn, "img1", 0.5, 0.0, 1.0)
    assert fresh_cache.cache_size() == 1

    ret = sd_models.unload_model_weights("a", b=2)

    assert ret == "freed"  # original return value preserved
    assert calls["count"] == 1  # original ran exactly once
    assert calls["args"] == ("a",)  # *args forwarded
    assert calls["kwargs"] == {"b": 2}  # **kwargs forwarded
    assert fresh_cache.cache_size() == 0  # cache cleared by the hook


def test_install_unload_hook_is_idempotent(monkeypatch):
    """A second install must detect the existing wrap and not double-wrap."""
    from modules.cplugapi import controlnet_cache

    sd_models, _ = _install_fake_sd_models(monkeypatch)

    assert controlnet_cache.install_unload_hook() is True
    wrapped_once = sd_models.unload_model_weights
    assert controlnet_cache.install_unload_hook() is False  # already wrapped
    assert sd_models.unload_model_weights is wrapped_once  # not re-wrapped


def test_install_unload_hook_fails_soft_without_symbol(monkeypatch):
    """If modules.sd_models lacks unload_model_weights, install returns
    False rather than raising — bootstrap must not crash."""
    from modules.cplugapi import controlnet_cache

    sd_models = types.ModuleType("modules.sd_models")  # no unload symbol
    monkeypatch.setitem(sys.modules, "modules.sd_models", sd_models)

    assert controlnet_cache.install_unload_hook() is False
