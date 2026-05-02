"""Unit tests for ``modules.cplugapi.cuda_alloc`` (audit 02 Phase C)."""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def fresh_cuda_alloc():
    """Reload the module to reset ``_APPLIED_THIS_PROCESS`` between tests."""
    if "modules.cplugapi.cuda_alloc" in sys.modules:
        return importlib.reload(sys.modules["modules.cplugapi.cuda_alloc"])
    from modules.cplugapi import cuda_alloc as mod
    return mod


def test_noop_when_env_already_set(fresh_cuda_alloc, monkeypatch):
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "garbage_collection_threshold:0.6")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fresh_cuda_alloc, "_cuda_looks_available", lambda: True)
    assert fresh_cuda_alloc.configure_expandable_segments() is False
    # Operator's value must not be clobbered.
    import os
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "garbage_collection_threshold:0.6"


def test_applies_on_linux_with_cuda(fresh_cuda_alloc, monkeypatch):
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fresh_cuda_alloc, "_cuda_looks_available", lambda: True)
    assert fresh_cuda_alloc.configure_expandable_segments() is True
    import os
    assert os.environ["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"


def test_noop_on_non_linux(fresh_cuda_alloc, monkeypatch):
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(fresh_cuda_alloc, "_cuda_looks_available", lambda: True)
    assert fresh_cuda_alloc.configure_expandable_segments() is False
    import os
    assert "PYTORCH_CUDA_ALLOC_CONF" not in os.environ


def test_noop_when_no_cuda(fresh_cuda_alloc, monkeypatch):
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fresh_cuda_alloc, "_cuda_looks_available", lambda: False)
    assert fresh_cuda_alloc.configure_expandable_segments() is False


def test_idempotent_second_call(fresh_cuda_alloc, monkeypatch):
    monkeypatch.delenv("PYTORCH_CUDA_ALLOC_CONF", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fresh_cuda_alloc, "_cuda_looks_available", lambda: True)
    assert fresh_cuda_alloc.configure_expandable_segments() is True
    # Second call sees the env already set and returns False — but
    # the predicate still reports as active.
    assert fresh_cuda_alloc.configure_expandable_segments() is False
    assert fresh_cuda_alloc.expandable_segments_active() is True


def test_predicate_recognizes_operator_set_value(fresh_cuda_alloc, monkeypatch):
    monkeypatch.setenv(
        "PYTORCH_CUDA_ALLOC_CONF",
        "max_split_size_mb:128,expandable_segments:True",
    )
    assert fresh_cuda_alloc.expandable_segments_active() is True


def test_predicate_false_when_unrelated_value(fresh_cuda_alloc, monkeypatch):
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:128")
    assert fresh_cuda_alloc.expandable_segments_active() is False


def test_register_capability(fresh_cuda_alloc, monkeypatch, clean_capabilities):
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    fresh_cuda_alloc.register_capabilities()
    assert "runtime/expandable-segments" in clean_capabilities.enabled_capabilities()
