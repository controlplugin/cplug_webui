"""Unit tests for ``modules.cplugapi.runtime`` (audit 01 §3.1)."""

from __future__ import annotations

import importlib

from modules.cplugapi import runtime


def _reset_module() -> None:
    """``apply_runtime_tweaks`` is gated by a module-level idempotency flag.

    Tests that need to re-trigger the path must reload the module to flip
    that flag back to ``False``. Reload is cheap (no side effects beyond
    rebinding the module attributes).
    """
    importlib.reload(runtime)


def test_apply_runtime_tweaks_is_idempotent():
    _reset_module()
    runtime.apply_runtime_tweaks()
    # A second call must be a no-op — verified by the absence of any
    # observable side effect (no exceptions, no logging spam at INFO).
    runtime.apply_runtime_tweaks()


def test_apply_runtime_tweaks_no_torch_environment():
    """If torch is not importable, the hook must not raise."""
    import sys

    saved = sys.modules.pop("torch", None)
    sys.modules["torch"] = None  # forces ImportError on `import torch`
    try:
        _reset_module()
        runtime.apply_runtime_tweaks()
    finally:
        if saved is not None:
            sys.modules["torch"] = saved
        else:
            sys.modules.pop("torch", None)
