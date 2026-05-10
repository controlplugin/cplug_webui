"""Cache the patched UNet clone produced by ``apply_controlnet_advanced``.

Background — the bug we're fixing
---------------------------------

``backend/patcher/controlnet.py:apply_controlnet_advanced`` runs every
gen when ControlNet is enabled. Its core sequence::

    cnet = controlnet.copy().set_cond_hint(image_bchw, strength, ...)
    m = unet.clone()             # NEW UnetPatcher instance per gen
    m.add_patched_controlnet(cnet)
    return m

Forge's ``backend/memory_management.py:load_models_gpu`` looks up the
incoming patcher in ``current_loaded_models`` via
``LoadedModel.__eq__`` (``self.model is other.model`` — patcher
*identity* check). The fresh clone from ``unet.clone()`` is a brand-
new object every gen, so the lookup misses unconditionally. The miss
triggers the clone-cleanup path at lines 642-650:

  1. The OLD patcher is detached (releases its hooks from the
     underlying ``nn.Module``).
  2. Finalizer is detached.
  3. The NEW patcher is loaded fresh — re-attaches the same hooks.

The underlying weights never leave VRAM, but the detach/reattach walk
costs **~1 second per gen** under our setup (Illustrious-XL on a 24 GB
card with the Xinsir Union ProMax ControlNet). It also generates the
spammy log triplet on every gen::

    Reusing ControlNet Model...
    Requested to load ControlNet
    Requested to load KModel
    loaded completely; ... 2396.80 MB loaded, full load: True
    Moving model(s) has taken 0.98 seconds

For a live-sketching workflow that fires several gens per minute,
that's a real per-stroke tax. Each line is also misleading — neither
weights nor patcher are *actually* being loaded fresh.

Why we don't fix this in ``backend/`` directly
----------------------------------------------

CLAUDE.md hard invariant 2: all fork code lives in ``modules/cplugapi/``
to keep the upstream rebase surface clean. ``backend/patcher/controlnet.py``
is shared with upstream Forge Neo. We monkey-patch from this module
instead, mirroring the pattern in :mod:`memmgmt_patches` (which guards
``LoadedModel.is_dead`` against upstream issue #694).

Why we don't fix this at the ``LoadedModel.__eq__`` layer
---------------------------------------------------------

That was the first design we considered — broaden equality to compare
underlying-model identity instead of patcher identity, so the
clone-cleanup path stops firing for any patcher (ToMe, ControlNet,
LoRA, etc.). Audit found a correctness break: with broadened equality,
the new patcher (with the new ControlNet ``cond_hint``) gets discarded
in favor of the old patcher already in ``current_loaded_models``,
which is then detached and reloaded **with its old hooks**. The new
sketch image silently disappears from the conditioning. See
``devlog/2026-05-09-controlnet-patcher-cache.md`` for the full trace.

The targeted cache here keeps patcher identity stable across gens (so
the existing equality logic continues to work as designed) and updates
the per-gen state in place on the cached ``cnet``.

Cache design
------------

* **Key**: ``(id(unet_baseline), _stable_controlnet_id(controlnet))``. The
  unet identity is stable across gens because Forge resets
  ``forge_objects.unet`` to the LoRA-applied snapshot at the start of
  each gen, AND our sibling ToMe cache (``modules/sd_models.py:apply_token_merging``)
  returns the same patched clone instance on hit. So ``id(forge_objects.unet)``
  agrees across gens.

  ``controlnet`` is trickier. Forge's controlnet integration layer
  (``extensions-builtin/sd_forge_controlnet/scripts/controlnet.py:378``)
  calls ``try_load_supported_control_model`` every gen, which calls
  ``try_build_from_state_dict`` (``modules_forge/supported_controlnet.py:80,203``)
  and returns a FRESH ``ControlNet`` / ``ControlLora`` / ``T2IAdapter``
  wrapper every time. The underlying weights (cldm.ControlNet) ARE
  cached in ``_CONTROL_MODEL_CACHE`` keyed on ``ckpt_path``, but the
  wrapper around them is new per gen — so ``id(controlnet)`` is unstable
  and would miss the cache every gen.

  :func:`_stable_controlnet_id` walks to the inner cached weights model
  (``controlnet.control_model`` / ``controlnet.control_weights`` /
  ``controlnet.t2i_model`` depending on subclass) and uses ITS id. That
  is the truly stable identity — same across gens for as long as the
  user doesn't swap ControlNet checkpoint.

  Both keys change in the right places:

    - LoRA application rebuilds ``forge_objects_after_applying_lora.unet``
      — new ``id()`` for the unet baseline → cache miss → fresh patch.
    - The user swaps checkpoint or ControlNet model — the inner weights
      object identity changes (different cldm.ControlNet instance) → miss.
    - The user adds another ControlNet to the stack — the first call's
      output becomes the second call's ``unet`` argument, so its key is
      ``(id(m_with_cn1), inner_id(cn2))`` — distinct from a single-CN call.
      Stacked CNs work correctly without special-casing.

* **Value**: the cached cloned UNet patcher (``m``) plus a weakref guard
  back to each baseline. The weakrefs handle ID reuse: Python may
  recycle an ``id()`` after the object is GC'd, so we verify on lookup
  that ``weak_baseline() is current_baseline``. If the weakref is dead
  or points to a different object, we treat the entry as stale.

* **On cache hit**: mutate the *existing* cnet linked into the cached m.
  We do NOT call ``add_patched_controlnet`` again — that would append a
  duplicate to ``controlnet_linked_list`` and the second add would
  shadow the first via ``previous_controlnet``. Single-CN per cache
  entry is the contract.

* **On cache miss**: invoke the original
  ``apply_controlnet_advanced``. Stash the result + weakrefs.

* **Cache size**: bounded by ``_MAX_ENTRIES`` (16). Realistic usage is
  1-3 entries (one model, maybe two ControlNets stacked). FIFO eviction
  on overflow — LRU is overkill for this size.

Failure modes & rollback
------------------------

If the cache ever serves a stale ``cnet`` (wrong ``cond_hint`` for the
current gen), every test in this module would have caught it. But to
be safe in production:

* Set env var ``CPLUG_CONTROLNET_CACHE=0`` to disable. Read once at
  install — flips the patch into a transparent passthrough.
* Capability advertisement is gated on the same flag, so a client can
  detect whether the optimisation is live.

The module is idempotent (class-level flag stamped on the upstream
``backend.patcher.controlnet`` module) so a webui reload doesn't
double-wrap.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from typing import Optional

_log = logging.getLogger(__name__)
try:
    from backend.logging import setup_logger as _setup_logger
    _setup_logger(_log)
except ImportError:
    pass

# Env-var kill switch. Read once at install time. Operators flipping it
# need a webui restart — same posture as the rest of cplugapi's
# installable knobs.
_ENV_DISABLE = "CPLUG_CONTROLNET_CACHE"

# Marker stamped on the upstream module so we don't double-wrap on
# webui reload. Class-level / module-level attributes survive
# re-imports because Python caches modules in ``sys.modules``.
_INSTALL_FLAG = "_cplugapi_controlnet_cache_installed"

# Cache state — protected by a lock for the (unlikely) case of two
# parallel gens entering ``apply_controlnet_advanced`` at once. With
# Forge's ``queue_lock`` serialising gens this rarely happens, but we
# pay one cheap mutex per call to remove the question.
_cache_lock = threading.Lock()
_cache: dict[tuple[int, int], "_CacheEntry"] = {}
_MAX_ENTRIES = 16


class _CacheEntry:
    """One row in the cache: cached patched UNet + baseline weakrefs.

    The weakrefs let us detect ``id()`` reuse: Python recycles the
    integer id of a GC'd object, so a stale key could otherwise serve
    a wrong cached value. Holding a weakref to the baselines lets us
    verify ``weak() is current_baseline`` on lookup; mismatch means
    the original baseline is gone and the entry is stale.

    Note: the controlnet weakref points at the INNER weights object
    (cldm.ControlNet / control_weights dict / t2i_model), not at the
    wrapper. The wrapper is rebuilt per gen by Forge so it would die
    immediately and the entry would never serve a hit. The inner
    weights are cached in ``_CONTROL_MODEL_CACHE`` and persist for the
    fork's lifetime.
    """

    __slots__ = ("unet_ref", "controlnet_inner_ref", "cached_unet")

    def __init__(self, unet, controlnet_inner, cached_unet):
        # weakref.ref may raise TypeError for a few exotic types
        # (instances of classes without __weakref__ slot). UnetPatcher
        # and cldm.ControlNet don't have that constraint, but defend
        # anyway: if the weakref can't be created, the entry is
        # unusable — caller falls back to fresh patch. ``control_weights``
        # for ControlLora is a plain dict which doesn't support
        # weakref; in that case ``_safe_ref`` returns None and the
        # entry is treated as immediately stale (no caching for that
        # subclass — acceptable correctness/perf tradeoff).
        self.unet_ref = _safe_ref(unet)
        self.controlnet_inner_ref = _safe_ref(controlnet_inner)
        # Strong reference. Keeps the cached patched UNet alive across
        # gens; Forge's memory_management may still evict its weights
        # from VRAM under pressure, but the patcher object stays in
        # ``current_loaded_models`` and the equality lookup hits.
        self.cached_unet = cached_unet

    def is_alive(self, unet, controlnet_inner) -> bool:
        """True iff both baselines are still the same objects we cached.

        ``id()`` keys can collide after GC; the weakref pair guarantees
        we only serve a hit when the original objects are reachable.
        """
        if self.unet_ref is None or self.controlnet_inner_ref is None:
            return False
        return (
            self.unet_ref() is unet
            and self.controlnet_inner_ref() is controlnet_inner
        )


def _safe_ref(obj) -> Optional["weakref.ReferenceType"]:
    try:
        return weakref.ref(obj)
    except TypeError:
        return None


# Field names checked in priority order to find the stable inner-weights
# identity for the controlnet wrapper. ``ControlNet`` exposes
# ``control_model`` (the cldm.ControlNet from _CONTROL_MODEL_CACHE);
# ``ControlLora`` and ``T2IAdapter`` expose their own equivalents. Order
# matters only for performance (most common subclass first).
_CONTROLNET_INNER_FIELDS: tuple[str, ...] = (
    "control_model",   # ControlNet
    "control_weights", # ControlLora
    "t2i_model",       # T2IAdapter
)


def _resolve_controlnet_inner(controlnet):
    """Return the cached inner-weights object backing ``controlnet``.

    Forge rebuilds the ``ControlNet`` / ``ControlLora`` / ``T2IAdapter``
    wrapper every gen via ``try_build_from_state_dict``, even when the
    underlying weights come from ``_CONTROL_MODEL_CACHE``. Keying our
    cache on the wrapper would miss every gen.

    Walk to the cached inner-weights object instead. Falls back to
    ``controlnet`` itself when no known field matches — for unknown
    subclasses that's equivalent to no caching (the wrapper dies
    immediately and ``is_alive()`` rejects the entry), which is the
    correct conservative behaviour for an unrecognised type.
    """
    for field in _CONTROLNET_INNER_FIELDS:
        weights = getattr(controlnet, field, None)
        if weights is not None:
            return weights
    return controlnet


def _stable_controlnet_id(controlnet) -> int:
    """``id()`` of the inner-weights object — kept as a separate helper
    so external code (tests, future capability probes) can introspect
    the key strategy without needing the wrapper.
    """
    return id(_resolve_controlnet_inner(controlnet))


def _is_enabled() -> bool:
    """``CPLUG_CONTROLNET_CACHE=0`` (or false/no/off) disables the cache.

    Default ON — the perf win is real and the correctness story is
    covered by tests.
    """
    raw = os.environ.get(_ENV_DISABLE)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# Captured at install time — the hot path doesn't pay an env-var lookup
# per gen. Mirrors the access_log / gen_timing pattern.
_emission_enabled: bool = True


def _mutate_cnet_for_gen(
    cnet,
    image_bchw,
    strength: float,
    start_percent: float,
    end_percent: float,
    positive_advanced_weighting,
    negative_advanced_weighting,
    advanced_frame_weighting,
    advanced_sigma_weighting,
    advanced_mask_weighting,
    control_type,
) -> None:
    """Update the cached ``cnet`` with this gen's per-call state.

    Mirrors the assignments inside the original
    ``apply_controlnet_advanced`` (``backend/patcher/controlnet.py:67-78``):
    ``set_cond_hint`` + ``set_control_type`` + the five
    ``advanced_*_weighting`` attribute writes. We mutate the SAME cnet
    object that was attached during the first patch — so the linked
    list in ``cached_unet.controlnet_linked_list`` continues to point
    at the right thing.

    No defensive copies of ``image_bchw``: ``set_cond_hint`` stores
    the tensor by reference, and the upstream code does the same. If
    the caller hands us a tensor that's about to be mutated, that's
    a caller-side bug either way.
    """
    cnet.set_cond_hint(image_bchw, strength, (start_percent, end_percent))
    cnet.set_control_type(control_type)
    cnet.positive_advanced_weighting = positive_advanced_weighting
    cnet.negative_advanced_weighting = negative_advanced_weighting
    cnet.advanced_frame_weighting = advanced_frame_weighting
    cnet.advanced_sigma_weighting = advanced_sigma_weighting
    cnet.advanced_mask_weighting = advanced_mask_weighting


def _evict_one_if_needed() -> None:
    """Cap cache size at ``_MAX_ENTRIES``. FIFO eviction.

    Realistic usage is 1-3 entries; this is belt-and-suspenders for
    workflows that swap models or stack many ControlNets. LRU would
    require touch-on-access bookkeeping; FIFO is enough for a 16-slot
    cap. ``dict`` preserves insertion order in CPython 3.7+ so
    ``next(iter(_cache))`` is the oldest key.
    """
    if len(_cache) <= _MAX_ENTRIES:
        return
    oldest = next(iter(_cache))
    del _cache[oldest]


def _build_wrapped_apply(original):
    """Build the wrapped ``apply_controlnet_advanced``.

    Closes over ``original`` so the wrapped function can fall through
    to upstream behaviour on cache miss + when the cache is disabled.
    """

    def wrapped(
        unet,
        controlnet,
        image_bchw,
        strength,
        start_percent,
        end_percent,
        positive_advanced_weighting=None,
        negative_advanced_weighting=None,
        advanced_frame_weighting=None,
        advanced_sigma_weighting=None,
        advanced_mask_weighting=None,
        control_type=None,
    ):
        # Disabled → pure passthrough so the env-var kill switch
        # restores upstream semantics exactly. We don't even take the
        # lock; the cache is dormant.
        if not _emission_enabled:
            return original(
                unet, controlnet, image_bchw, strength, start_percent, end_percent,
                positive_advanced_weighting=positive_advanced_weighting,
                negative_advanced_weighting=negative_advanced_weighting,
                advanced_frame_weighting=advanced_frame_weighting,
                advanced_sigma_weighting=advanced_sigma_weighting,
                advanced_mask_weighting=advanced_mask_weighting,
                control_type=control_type,
            )

        # Walk to the cached inner-weights object — the controlnet
        # wrapper itself is rebuilt every gen by
        # ``try_build_from_state_dict``, but the inner weights come from
        # ``_CONTROL_MODEL_CACHE`` and are stable across gens. See
        # :func:`_stable_controlnet_id` for the field-walk rationale.
        controlnet_inner = _resolve_controlnet_inner(controlnet)
        key = (id(unet), id(controlnet_inner))
        with _cache_lock:
            entry = _cache.get(key)
            if entry is not None and entry.is_alive(unet, controlnet_inner):
                # Cache hit. Mutate the cnet already linked into the
                # cached patcher's controlnet_linked_list. The list
                # head is the cnet we attached during the first patch
                # for this baseline pair (single-CN-per-entry contract).
                cached = entry.cached_unet
                cnet = cached.controlnet_linked_list
                if cnet is None:
                    # Defensive: a stray external clear of the list
                    # would land here. Treat as miss + rebuild.
                    del _cache[key]
                else:
                    _mutate_cnet_for_gen(
                        cnet,
                        image_bchw,
                        strength,
                        start_percent,
                        end_percent,
                        positive_advanced_weighting,
                        negative_advanced_weighting,
                        advanced_frame_weighting,
                        advanced_sigma_weighting,
                        advanced_mask_weighting,
                        control_type,
                    )
                    return cached
            elif entry is not None:
                # Stale entry (one of the baselines was GC'd and its id
                # may now refer to a different object). Drop it and
                # rebuild below.
                del _cache[key]

        # Cache miss — fall through to the original. Holding the lock
        # during the original call would serialise gens unnecessarily;
        # release it, run the original, then reacquire to install the
        # new entry. Concurrent miss for the same key is harmless: both
        # callers patch fresh, the second install overwrites the first,
        # and the loser's patched UNet is GC'd because nothing else
        # references it.
        result = original(
            unet, controlnet, image_bchw, strength, start_percent, end_percent,
            positive_advanced_weighting=positive_advanced_weighting,
            negative_advanced_weighting=negative_advanced_weighting,
            advanced_frame_weighting=advanced_frame_weighting,
            advanced_sigma_weighting=advanced_sigma_weighting,
            advanced_mask_weighting=advanced_mask_weighting,
            control_type=control_type,
        )

        # Tag the cnet linked into the result so our cleanup wrapper
        # (see :func:`_build_wrapped_cleanup`) knows to skip the
        # ``unload_model`` call at sampling_cleanup time. Without this
        # tag, every gen ends with ``cnet.cleanup()`` popping the cnet's
        # ``control_model_wrapped`` from ``current_loaded_models``, and
        # the next gen's ``load_models_gpu`` lookup misses → a noisy
        # "Requested to load ControlNet" log line + redundant LoadedModel
        # construction. The tag scopes the skip to OUR cached cnets only —
        # a non-cached cnet's cleanup proceeds normally and stays
        # correct under checkpoint swaps.
        cached_cnet = result.controlnet_linked_list
        if cached_cnet is not None:
            cached_cnet._cplug_cache_held = True

        with _cache_lock:
            _cache[key] = _CacheEntry(unet, controlnet_inner, result)
            _evict_one_if_needed()

        return result

    wrapped.__name__ = "apply_controlnet_advanced"
    wrapped.__qualname__ = "apply_controlnet_advanced"
    wrapped.__doc__ = (
        "cplugapi-wrapped ``apply_controlnet_advanced``: caches the "
        "patched UNet clone keyed on (id(unet), id(controlnet)) so "
        "back-to-back gens skip the ~1 s clone-cleanup walk in "
        "``load_models_gpu``. See modules/cplugapi/controlnet_cache.py "
        "for the full design. Mutates per-gen state on cache hit."
    )
    return wrapped


def _build_wrapped_cleanup(original_cleanup):
    """Wrap ``ControlNet.cleanup`` to skip ``unload_model`` for cnets
    we hold in cache.

    Why this exists: the cache is necessary but not sufficient. Even
    when ``apply_controlnet_advanced`` returns the same UnetPatcher
    across gens (KModel detach/reattach skipped), Forge's
    ``sampling_cleanup`` still calls ``cnet.cleanup()`` at the end of
    every gen, which calls
    ``memory_management.unload_model(self.control_model_wrapped)`` and
    pops the cnet's wrapped patcher from ``current_loaded_models``.
    Next gen's ``load_models_gpu`` lookup misses → "Requested to load
    ControlNet" log fires → entry re-inserted (cheap, no weight
    movement, but the log is noise + the LoadedModel construction is
    wasted churn).

    The skip is scoped to cnets tagged with ``_cplug_cache_held``
    (set when we install the entry). Untagged cnets — those produced
    by a non-cached call path or from a competing extension — clean
    up normally so we don't break their lifecycle.

    We still need ``ControlBase.cleanup`` semantics: clearing
    ``cond_hint`` / ``timestep_range`` keeps the cnet ready for the
    next gen's mutate-on-hit (set_cond_hint will repopulate them).
    The simplest safe path is to invoke the original cleanup but
    intercept the ``memory_management.unload_model`` call on tagged
    cnets — done by temporarily swapping that symbol on the
    ``backend.memory_management`` module while the original runs.
    See :func:`_invoke_cleanup_with_unload_skipped`.
    """

    def wrapped_cleanup(self):
        if not getattr(self, "_cplug_cache_held", False):
            return original_cleanup(self)
        return _invoke_cleanup_with_unload_skipped(original_cleanup, self)

    wrapped_cleanup.__name__ = "cleanup"
    wrapped_cleanup.__qualname__ = "ControlNet.cleanup"
    wrapped_cleanup.__doc__ = (
        "cplugapi-wrapped ``ControlNet.cleanup``: skips the "
        "``unload_model`` call for cnets held by the patcher cache "
        "(tagged with ``_cplug_cache_held``). All other cleanup work "
        "(cond_hint clear, model_sampling_current reset) still runs."
    )
    return wrapped_cleanup


def _invoke_cleanup_with_unload_skipped(original_cleanup, self):
    """Run ``original_cleanup`` with ``memory_management.unload_model``
    short-circuited to a no-op.

    Why this dance instead of just reimplementing the cleanup logic
    inline: the original is a small but real function that may evolve
    upstream (e.g., a future Forge change might add additional cleanup
    steps inside ``ControlNet.cleanup``). Reimplementing it forks the
    behavior. Swapping out the single offending symbol — ``unload_model``
    — keeps us aligned with upstream semantics for everything else.

    The swap is local to this call (try/finally restore) so concurrent
    cleanups don't see a corrupt module state. Forge's ``queue_lock``
    serialises gens anyway, but defensive isolation is cheap.
    """
    try:
        from backend import memory_management
    except Exception:
        # Backend unreachable (test path). The original cleanup will
        # also fail, so let it raise normally.
        return original_cleanup(self)

    sentinel_unload = memory_management.unload_model
    memory_management.unload_model = _noop_unload
    try:
        return original_cleanup(self)
    finally:
        # Only restore if our sentinel still in place — a competing
        # patch in the meantime would have its own value, don't clobber.
        if memory_management.unload_model is _noop_unload:
            memory_management.unload_model = sentinel_unload


def _noop_unload(model) -> bool:
    """Drop-in replacement for ``memory_management.unload_model``
    during cached-cnet cleanup. Returns ``False`` to signal "nothing
    was unloaded", matching the contract for a model that wasn't in
    ``current_loaded_models``."""
    return False


def _resolve_target_module():
    """Import and return ``backend.patcher.controlnet``.

    Returns ``None`` when the backend isn't importable (cplugapi unit
    tests stub it out). Caller treats ``None`` as a no-op signal.
    """
    try:
        from backend.patcher import controlnet  # type: ignore
    except Exception:
        return None
    return controlnet


# Modules that imported ``apply_controlnet_advanced`` BY NAME via
# ``from backend.patcher.controlnet import apply_controlnet_advanced``.
# Python's ``from X import Y`` copies the reference at import time, so
# rebinding ``X.Y`` later does NOT update ``the_module.Y``. To make the
# wrapper actually intercept production calls we have to rebind each
# consumer's local copy too. Add new consumers here when grep finds
# them.
#
# Identified via:
#     grep -rn "from backend.patcher.controlnet import" .
#     grep -rn "from backend.patcher import controlnet" .  # qualified — safe
_KNOWN_CONSUMER_MODULES: tuple[str, ...] = (
    "modules_forge.supported_controlnet",
)


def apply(target_module=None) -> bool:
    """Wrap ``apply_controlnet_advanced`` on the target module.

    Parameters
    ----------
    target_module:
        The module to patch. ``None`` resolves
        ``backend.patcher.controlnet`` at call time. Tests pass a
        synthetic module to exercise the wrapper without dragging in
        torch.

    Returns
    -------
    bool
        True iff this call newly installed the wrapper. False on a
        re-install (idempotent) or when the target module isn't
        resolvable.
    """
    global _emission_enabled
    _emission_enabled = _is_enabled()

    if target_module is None:
        target_module = _resolve_target_module()
    if target_module is None:
        return False

    if getattr(target_module, _INSTALL_FLAG, False):
        return False

    original = getattr(target_module, "apply_controlnet_advanced", None)
    if original is None:
        # Upstream refactored the symbol away — log loudly so the
        # mismatch surfaces during the next webui boot rather than
        # silently leaving the perf bug in place.
        _log.warning(
            "cplugapi: %s has no apply_controlnet_advanced; cache not installed",
            target_module.__name__,
        )
        return False

    wrapped = _build_wrapped_apply(original)
    target_module.apply_controlnet_advanced = wrapped
    setattr(target_module, _INSTALL_FLAG, True)

    # Rebind known consumers that imported the symbol BY NAME via
    # ``from backend.patcher.controlnet import apply_controlnet_advanced``.
    # Without this the production call at
    # ``modules_forge/supported_controlnet.py:207`` continues to invoke
    # the ORIGINAL (the local copy captured at import time), the cache
    # never fires, and the perf bug remains hidden behind a "patched"
    # log line. See _KNOWN_CONSUMER_MODULES.
    rebound = _rebind_known_consumers(original, wrapped)

    # Sibling patch: skip ``unload_model`` in ``ControlNet.cleanup``
    # for cnets we hold. Without this the cache is necessary but not
    # sufficient — sampling_cleanup at end-of-gen still pops the cnet's
    # wrapped patcher from ``current_loaded_models``, producing a noisy
    # "Requested to load ControlNet" line on the next gen.
    cleanup_patched = _patch_controlnet_cleanup(target_module)

    _log.info(
        "cplugapi: patched %s.apply_controlnet_advanced (controlnet patcher cache, %s; rebound %d consumer(s); cleanup-skip %s)",
        target_module.__name__,
        "enabled" if _emission_enabled else "disabled — passthrough",
        rebound,
        "installed" if cleanup_patched else "not installed",
    )
    return True


_CLEANUP_PATCHED_FLAG = "_cplugapi_controlnet_cleanup_patched"


def _patch_controlnet_cleanup(target_module) -> bool:
    """Wrap ``ControlNet.cleanup`` on the target module's class.

    Idempotent via ``_CLEANUP_PATCHED_FLAG`` stamped on the class.
    Returns True iff this call newly installed the wrapper.
    """
    cls = getattr(target_module, "ControlNet", None)
    if cls is None:
        _log.warning(
            "cplugapi: %s has no ControlNet class; cleanup-skip not installed",
            target_module.__name__,
        )
        return False
    if getattr(cls, _CLEANUP_PATCHED_FLAG, False):
        return False

    original_cleanup = cls.cleanup
    cls.cleanup = _build_wrapped_cleanup(original_cleanup)
    setattr(cls, _CLEANUP_PATCHED_FLAG, True)
    return True


def _rebind_known_consumers(original, wrapped) -> int:
    """For each ``from backend.patcher.controlnet import apply_controlnet_advanced``
    consumer, rebind its local symbol to ``wrapped`` if (and only if)
    it currently points at ``original``.

    The identity guard matters: if a consumer rebound the symbol itself
    (some test fixture, a competing patch, etc.) we don't clobber.
    Returns the count of consumers actually rebound, for logging.
    """
    import sys

    rebound = 0
    for module_name in _KNOWN_CONSUMER_MODULES:
        module = sys.modules.get(module_name)
        if module is None:
            # Consumer not loaded yet. The patch will still take effect
            # if the consumer hasn't imported anything — its later
            # ``from`` will see our wrapped function on the source. The
            # gotcha is consumers that loaded BEFORE us; for them we
            # need to be in their namespace already, which means this
            # rebind has to run after the consumer imports.
            continue
        current = getattr(module, "apply_controlnet_advanced", None)
        if current is original:
            module.apply_controlnet_advanced = wrapped
            rebound += 1
        elif current is wrapped:
            # Already rebound by a prior call (idempotent).
            pass
        elif current is None:
            # Consumer module exists but didn't import the symbol.
            # Possible if the consumer used the qualified form
            # (``from backend.patcher import controlnet`` then
            # ``controlnet.apply_controlnet_advanced(...)``) — in which
            # case our source-module patch is sufficient. No work to do.
            pass
        else:
            # Consumer has its own version (rare — competing extension?).
            # Don't clobber. Surface the divergence so a future
            # debugger sees the missed coverage.
            _log.warning(
                "cplugapi: %s.apply_controlnet_advanced is not the upstream "
                "function (got %r); cache wrapper not rebound there",
                module_name,
                current,
            )
    return rebound


def is_applied(target_module=None) -> bool:
    """Test-only: surface whether the patch is installed."""
    if target_module is None:
        target_module = _resolve_target_module()
    if target_module is None:
        return False
    return bool(getattr(target_module, _INSTALL_FLAG, False))


def reset_cache_for_tests() -> None:
    """Clear the cache. Test-only escape hatch.

    Production has no use for this — entries naturally evict on LoRA
    swap / model swap / cap overflow.
    """
    with _cache_lock:
        _cache.clear()


def cache_size() -> int:
    """Test-only: number of entries currently held."""
    with _cache_lock:
        return len(_cache)


def register_capabilities() -> None:
    """Advertise the optimisation when enabled."""
    if not _is_enabled():
        return
    from . import capabilities

    capabilities.register("controlnet/patcher-cache")
