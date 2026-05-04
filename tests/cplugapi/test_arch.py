"""Unit tests for ``modules.cplugapi.arch.classify_state_keys``.

Real key sets are extracted from public checkpoint headers, but for
Phase 1 we exercise the classifier with hand-crafted but ComfyUI-
aligned key signatures (sourced from comfy/model_detection.py and
comfy/supported_models.py). The fixtures live inline here rather than
as JSON files because the must-have set ended up small enough that
inline parametrize is more readable than a fixture-loader indirection.
"""

from __future__ import annotations

import pytest

from modules.cplugapi import arch


# Each row: (label, keys, metadata, expected). Metadata is most often
# None — the SAI fast-path is exercised separately so its rows don't
# crowd this table.
_CASES = [
    pytest.param(
        "sd15",
        {
            "model.diffusion_model.input_blocks.0.0.weight",
            "model.diffusion_model.output_blocks.11.0.skip_connection.weight",
            "cond_stage_model.transformer.text_model.embeddings.token_embedding.weight",
            "first_stage_model.encoder.conv_in.weight",
        },
        None,
        arch.ARCH_SD15,
        id="sd15",
    ),
    pytest.param(
        "sd2",
        {
            "model.diffusion_model.input_blocks.0.0.weight",
            "cond_stage_model.model.transformer.resblocks.0.attn.in_proj_weight",
            "first_stage_model.encoder.conv_in.weight",
        },
        None,
        arch.ARCH_SD2,
        id="sd2",
    ),
    pytest.param(
        "sdxl",
        {
            "model.diffusion_model.input_blocks.0.0.weight",
            "conditioner.embedders.0.transformer.text_model.embeddings.token_embedding.weight",
            "conditioner.embedders.1.model.transformer.resblocks.9.mlp.c_proj.bias",
            "first_stage_model.encoder.conv_in.weight",
        },
        None,
        arch.ARCH_SDXL,
        id="sdxl",
    ),
    pytest.param(
        "sdxl_refiner",
        {
            "model.diffusion_model.input_blocks.0.0.weight",
            "conditioner.embedders.0.model.transformer.resblocks.9.mlp.c_proj.bias",
            "first_stage_model.encoder.conv_in.weight",
        },
        None,
        arch.ARCH_SDXL_REFINER,
        id="sdxl_refiner",
    ),
    pytest.param(
        "flux_dev",
        {
            "double_blocks.0.img_attn.qkv.weight",
            "img_in.weight",
            "guidance_in.in_layer.weight",
            "single_blocks.0.linear1.weight",
        },
        None,
        arch.ARCH_FLUX,
        id="flux_dev",
    ),
    pytest.param(
        "flux_schnell",
        {
            "double_blocks.0.img_attn.qkv.weight",
            "img_in.weight",
            # NO guidance_in — schnell has no guidance distillation
            "single_blocks.0.linear1.weight",
        },
        None,
        arch.ARCH_FLUX_SCHNELL,
        id="flux_schnell",
    ),
    pytest.param(
        "sd3",
        {
            "joint_blocks.0.context_block.attn.qkv.weight",
            "x_embedder.proj.weight",
            "context_embedder.weight",
        },
        None,
        arch.ARCH_SD3,
        id="sd3",
    ),
    pytest.param(
        "pixart_alpha",
        {
            "t_block.1.weight",
            "blocks.0.scale_shift_table",
        },
        None,
        arch.ARCH_PIXART_ALPHA,
        id="pixart_alpha",
    ),
    pytest.param(
        "lumina2",
        {
            "cap_embedder.1.weight",
            "noise_refiner.0.attention.k_norm.weight",
        },
        None,
        arch.ARCH_LUMINA2,
        id="lumina2",
    ),
    pytest.param(
        "hunyuan_dit",
        {
            "mlp_t5.0.weight",
            "blocks.0.attn1.q_proj.weight",
        },
        None,
        arch.ARCH_HUNYUAN_DIT,
        id="hunyuan_dit",
    ),
    pytest.param(
        "unknown_random_keys",
        {"random.layer.weight", "another.thing.bias"},
        None,
        arch.ARCH_UNKNOWN,
        id="unknown",
    ),
    pytest.param(
        "lora_kohya",
        {
            "lora_unet_input_blocks_0_0.lora_up.weight",
            "lora_unet_input_blocks_0_0.lora_down.weight",
            "lora_unet_input_blocks_0_0.alpha",
        },
        None,
        arch.ARCH_NOT_A_CHECKPOINT,
        id="lora_kohya",
    ),
    pytest.param(
        "lora_peft",
        {
            "transformer.layers.0.attn.q_proj.lora_A.weight",
            "transformer.layers.0.attn.q_proj.lora_B.weight",
        },
        None,
        arch.ARCH_NOT_A_CHECKPOINT,
        id="lora_peft",
    ),
    pytest.param(
        "vae_only",
        {
            "encoder.conv_in.weight",
            "decoder.conv_in.weight",
            "quant_conv.weight",
        },
        None,
        arch.ARCH_NOT_A_CHECKPOINT,
        id="vae_only",
    ),
    pytest.param(
        "te_only",
        {
            "text_model.embeddings.token_embedding.weight",
            "text_model.encoder.layers.0.self_attn.q_proj.weight",
        },
        None,
        arch.ARCH_NOT_A_CHECKPOINT,
        id="te_only",
    ),
]


@pytest.mark.parametrize("label,keys,metadata,expected", _CASES)
def test_classify(label, keys, metadata, expected):
    assert arch.classify_state_keys(keys, metadata) == expected, label


def test_sai_metadata_fast_path_sdxl():
    """SAI Model Spec metadata short-circuits sentinel matching."""
    # Random keys that would otherwise classify as unknown
    keys = {"random.weight"}
    md = {"modelspec.architecture": "stable-diffusion-xl-v1-base"}
    assert arch.classify_state_keys(keys, md) == arch.ARCH_SDXL


def test_sai_metadata_unknown_string_falls_through():
    """An unmapped SAI string must not block — fall through to sentinels."""
    sd15_keys = {
        "model.diffusion_model.input_blocks.0.0.weight",
        "model.diffusion_model.output_blocks.11.0.skip_connection.weight",
        "cond_stage_model.transformer.text_model.embeddings.token_embedding.weight",
    }
    md = {"modelspec.architecture": "future-format-not-in-our-table"}
    assert arch.classify_state_keys(sd15_keys, md) == arch.ARCH_SD15


def test_arch_for_engine_known():
    class StableDiffusionXL:
        pass
    assert arch.arch_for_engine(StableDiffusionXL()) == arch.ARCH_SDXL


def test_arch_for_engine_unknown_class():
    class SomeFutureEngine:
        pass
    assert arch.arch_for_engine(SomeFutureEngine()) == arch.ARCH_UNKNOWN


def test_arch_for_engine_flux_returns_coarse():
    """The shared Flux engine class always maps to ARCH_FLUX (per D6)."""
    class Flux:
        pass
    assert arch.arch_for_engine(Flux()) == arch.ARCH_FLUX
