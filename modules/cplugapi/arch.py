"""Canonical model-architecture vocabulary for ``/cplugapi/v1/models/*``.

Single source of truth for the ``arch`` string returned by both
``/cplugapi/v1/models/active`` (engine-class lookup) and
``/cplugapi/v1/models/sd-checkpoints`` (header-key signature scan).

Two-tier vocabulary:

1. **Forge engine classes** — what the loader instantiates. Endpoint (1)
   maps ``engine.__class__.__name__`` through ``ENGINE_CLASS_TO_ARCH``.
   The ``Flux`` class is shared across flux-dev / flux-schnell, so
   endpoint (1) returns the coarse ``"flux"`` for both — endpoint (2)
   refines via header sentinels.

2. **Header-only arches** — formats Forge does not yet load (SD2, SD3,
   PixArt, Cascade, Hunyuan-DiT). Reachable only via endpoint (2).

Probe order in :func:`classify_state_keys` is "most specific first":
SAI metadata fast-path → LoRA filter → VAE-only filter → TE-only filter
→ DiT sentinels (Flux > SD3 > PixArt > Lumina-2 > Hunyuan-DiT > Cascade)
→ UNet sentinels (SDXL refiner > SDXL > SD2 > SD15) → ``unknown``.

Extending: a new arch label needs (a) an entry here, (b) a sentinel row
in :func:`classify_state_keys`, (c) a fixture under
``tests/cplugapi/fixtures/arch_keys/<arch>.json``. Labels with no
fixture are reserved as constants but never returned by the classifier.
"""

from __future__ import annotations

from typing import Iterable, Optional

# Arch label constants — referenced by tests + endpoint handlers.
ARCH_SD15 = "sd15"
ARCH_SD2 = "sd2"
ARCH_SDXL = "sdxl"
ARCH_SDXL_REFINER = "sdxl_refiner"
ARCH_SD3 = "sd3"
ARCH_FLUX = "flux"
ARCH_FLUX_SCHNELL = "flux_schnell"
ARCH_FLUX2 = "flux2"
ARCH_PIXART_ALPHA = "pixart_alpha"
ARCH_PIXART_SIGMA = "pixart_sigma"  # TODO: fixture
ARCH_LUMINA2 = "lumina2"
ARCH_HUNYUAN_DIT = "hunyuan_dit"
ARCH_CASCADE_B = "cascade_b"  # TODO: fixture
ARCH_CASCADE_C = "cascade_c"  # TODO: fixture
ARCH_WAN = "wan"  # TODO: fixture
ARCH_MUGEN = "mugen"  # TODO: fixture
ARCH_CHROMA = "chroma"  # TODO: fixture
ARCH_ZIMAGE = "zimage"  # TODO: fixture
ARCH_ANIMA = "anima"  # TODO: fixture
ARCH_ERNIE = "ernie"  # TODO: fixture
ARCH_QWEN = "qwen"  # TODO: fixture
ARCH_UNKNOWN = "unknown"
ARCH_NOT_A_CHECKPOINT = "not_a_checkpoint"


# Maps Forge engine ``__class__.__name__`` to a canonical arch label.
# Source: ``backend/diffusion_engine/*.py`` class definitions.
# ``Flux`` deliberately maps to ``ARCH_FLUX`` (coarse) — endpoint (1)
# cannot distinguish dev/schnell from a loaded engine instance.
ENGINE_CLASS_TO_ARCH: dict[str, str] = {
    "StableDiffusion": ARCH_SD15,
    "StableDiffusionXL": ARCH_SDXL,
    "StableDiffusionXLRefiner": ARCH_SDXL_REFINER,
    "Flux": ARCH_FLUX,
    "Flux2": ARCH_FLUX2,
    "Lumina2": ARCH_LUMINA2,
    "Wan": ARCH_WAN,
    "Mugen": ARCH_MUGEN,
    "Chroma": ARCH_CHROMA,
    "ZImage": ARCH_ZIMAGE,
    "Anima": ARCH_ANIMA,
    "ErnieImage": ARCH_ERNIE,
    "QwenImage": ARCH_QWEN,
}


# Maps SAI Model Spec ``modelspec.architecture`` strings (kohya-ss
# convention) to canonical arch labels. Empirically populated — fast-path
# miss falls through to signature matching, so an unmapped value never
# blocks. Extend as fixtures land.
SAI_ARCH_MAP: dict[str, str] = {
    "stable-diffusion-v1": ARCH_SD15,
    "stable-diffusion-xl-v1-base": ARCH_SDXL,
    # TODO: fixture — populate the rest from real kohya checkpoints
    "stable-diffusion-v2": ARCH_SD2,
    "stable-diffusion-xl-v1-refiner": ARCH_SDXL_REFINER,
    "stable-diffusion-3": ARCH_SD3,
}


# Strip these prefixes (in order) before testing sentinel keys. Comfy-
# format checkpoints prefix with ``model.diffusion_model.``; diffusers-
# format flattens; some single-file dumps use ``net.``.
_PREFIXES = ("model.diffusion_model.", "model.", "net.")


def _strip(key: str) -> str:
    """Strip recognised prefixes repeatedly. Some merged dumps carry
    a double prefix like ``model.diffusion_model.model.<...>``; one
    pass would leave ``model.<...>`` which would miss a sentinel that
    expects the bare form. Loop until no prefix matches."""
    while True:
        for p in _PREFIXES:
            if key.startswith(p):
                key = key[len(p):]
                break
        else:
            return key


# Pre-built sentinel sets — reused across every classify call. Module-
# level constants avoid rebuilding the frozenset on each invocation.
_FLUX_SENTINELS = frozenset((
    "double_blocks.0.img_attn.qkv.weight",
    "double_blocks.0.img_attn.norm.key_norm.weight",
    "img_in.weight",
))
_SD3_SENTINELS = frozenset((
    "joint_blocks.0.context_block.attn.qkv.weight",
    "x_embedder.proj.weight",
    "context_embedder.weight",
))
_PIXART_SENTINELS = frozenset((
    "t_block.1.weight",
    "blocks.0.scale_shift_table",
))
_LUMINA2_SENTINELS = frozenset((
    "cap_embedder.1.weight",
    "noise_refiner.0.attention.k_norm.weight",
))
_HUNYUAN_DIT_SENTINELS = frozenset(("mlp_t5.0.weight",))
_CASCADE_C_SENTINELS = frozenset(("clf.1.weight",))
_CASCADE_C_REQUIRED = frozenset(("clip_txt_mapper.weight",))
_CASCADE_B_SENTINELS = frozenset(("clip_mapper.weight",))

# LoRA suffix tells: any key ending with one of these is a LoRA adapter
# tensor. Used by the LoRA filter (after we've established there is no
# UNet/DiT body in the checkpoint).
_LORA_SUFFIXES = (
    ".lora_up.weight", ".lora_down.weight",
    ".lora_A.weight", ".lora_B.weight",
    ".alpha",
)

# Body-tensor prefixes used to decide "is this a full model or a
# component-only file?". Shared by the LoRA / VAE / TE filters.
_BODY_PREFIXES = (
    "input_blocks.", "output_blocks.",
    "double_blocks.", "single_blocks.",
    "joint_blocks.", "blocks.",
)


def arch_for_engine(engine: object) -> str:
    """Map a loaded Forge engine instance to a canonical arch label.

    Returns :data:`ARCH_UNKNOWN` for engines whose class isn't in
    :data:`ENGINE_CLASS_TO_ARCH`. Returns :data:`ARCH_UNKNOWN` for the
    ``FakeInitialModel`` placeholder (caller should check ``loaded``
    state before calling this).
    """
    cls_name = type(engine).__name__
    return ENGINE_CLASS_TO_ARCH.get(cls_name, ARCH_UNKNOWN)


def classify_state_keys(
    keys: Iterable[str],
    metadata: Optional[dict] = None,
) -> str:
    """Identify the architecture of a checkpoint from its state-dict keys.

    ``keys`` is an iterable of tensor names from a safetensors header.
    ``metadata`` is the ``__metadata__`` dict from the same header (or
    ``None`` if absent). The classifier never raises — corrupt or
    unrecognized inputs fall through to :data:`ARCH_UNKNOWN` /
    :data:`ARCH_NOT_A_CHECKPOINT`.
    """
    # Materialize once — we strip prefixes and probe multiple times.
    stripped = frozenset(_strip(k) for k in keys)

    # SAI Model Spec fast-path. Mapped value short-circuits; unmapped
    # value is benign — fall through.
    if metadata:
        sai = metadata.get("modelspec.architecture")
        if isinstance(sai, str):
            mapped = SAI_ARCH_MAP.get(sai)
            if mapped is not None:
                return mapped

    has_body = _has_body_tensors(stripped)

    # LoRA / VAE / TE component checks fire only when there is no UNet or
    # DiT body. A merged-but-not-fully-baked checkpoint may carry both a
    # full UNet AND stray ``.alpha`` keys; classifying it as
    # ``not_a_checkpoint`` would lose a perfectly usable model.
    if not has_body:
        if any(k.endswith(_LORA_SUFFIXES) for k in stripped):
            return ARCH_NOT_A_CHECKPOINT
        if _is_vae_only(stripped):
            return ARCH_NOT_A_CHECKPOINT
        if _is_te_only(stripped):
            return ARCH_NOT_A_CHECKPOINT

    # DiT-family sentinels — most specific first.
    if stripped & _FLUX_SENTINELS:
        # flux-dev distills a guidance scalar; flux-schnell does not.
        if "guidance_in.in_layer.weight" in stripped:
            return ARCH_FLUX
        return ARCH_FLUX_SCHNELL
    if stripped & _SD3_SENTINELS:
        return ARCH_SD3
    if stripped & _PIXART_SENTINELS:
        return ARCH_PIXART_ALPHA
    if stripped & _LUMINA2_SENTINELS:
        return ARCH_LUMINA2
    if stripped & _HUNYUAN_DIT_SENTINELS:
        return ARCH_HUNYUAN_DIT

    # UNet-family sentinels. SDXL refiner has a single text encoder
    # (embedders.0 = OpenCLIP-G); SDXL base adds embedders.1.
    if "input_blocks.0.0.weight" in stripped:
        has_openclip_g = (
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias"
            in stripped
        )
        has_g_only = (
            "conditioner.embedders.0.model.transformer.resblocks.9.mlp.c_proj.bias"
            in stripped
        )
        if has_openclip_g:
            return ARCH_SDXL
        if has_g_only:
            return ARCH_SDXL_REFINER
        # SD2 and SD15 share the input_blocks layout; SD2 carries
        # ``cond_stage_model.model.*`` (OpenCLIP), SD15 carries
        # ``cond_stage_model.transformer.text_model.*`` (CLIP-L).
        if any(k.startswith("cond_stage_model.model.") for k in stripped):
            return ARCH_SD2
        return ARCH_SD15

    # Stable Cascade — Stage C requires both clf + clip_txt_mapper;
    # Stage B has clip_mapper alone.
    if (stripped & _CASCADE_C_SENTINELS) and (stripped & _CASCADE_C_REQUIRED):
        return ARCH_CASCADE_C
    if stripped & _CASCADE_B_SENTINELS:
        return ARCH_CASCADE_B

    return ARCH_UNKNOWN


def _has_body_tensors(stripped: frozenset[str]) -> bool:
    """True if keys include UNet-style or DiT-style body tensors."""
    return any(k.startswith(_BODY_PREFIXES) for k in stripped)


def _is_vae_only(stripped: frozenset[str]) -> bool:
    """True if keys look like a VAE-only checkpoint (no UNet/DiT)."""
    return any(
        k.startswith(("encoder.", "decoder.", "quant_conv", "post_quant_conv"))
        for k in stripped
    )


def _is_te_only(stripped: frozenset[str]) -> bool:
    """True if keys look like a text-encoder-only checkpoint."""
    return any(
        k.startswith(("text_model.", "cond_stage_model.",
                       "conditioner.embedders."))
        for k in stripped
    )
