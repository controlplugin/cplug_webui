"""Unit tests for ``modules.cplugapi.megacache`` (audit 02 Phase C)."""

from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture
def fresh_megacache(tmp_path, monkeypatch):
    """Reload the module with ``_REPO_ROOT`` rebased onto ``tmp_path``.

    Reload also resets ``_LOADED_OK`` / ``_APPLIED`` / ``_ATEXIT_INSTALLED``
    so each test starts from a clean state.
    """
    # Strip the inductor env vars so ``configure_env`` exercises the
    # "set when unset" branch by default; tests that need the opposite
    # set them explicitly inside the test body.
    for key in ("TORCHINDUCTOR_FX_GRAPH_CACHE",
                "TORCHINDUCTOR_AUTOGRAD_CACHE",
                "TORCHINDUCTOR_CACHE_DIR"):
        monkeypatch.delenv(key, raising=False)

    if "modules.cplugapi.megacache" in sys.modules:
        mod = importlib.reload(sys.modules["modules.cplugapi.megacache"])
    else:
        from modules.cplugapi import megacache as mod
    monkeypatch.setattr(mod, "_REPO_ROOT", tmp_path)
    return mod


def test_configure_env_sets_when_unset(fresh_megacache, tmp_path, monkeypatch):
    fresh_megacache.configure_env()
    assert os.environ["TORCHINDUCTOR_FX_GRAPH_CACHE"] == "1"
    assert os.environ["TORCHINDUCTOR_AUTOGRAD_CACHE"] == "1"
    assert os.environ["TORCHINDUCTOR_CACHE_DIR"] == str(tmp_path / "cache" / "inductor")


def test_configure_env_respects_operator_override(fresh_megacache, monkeypatch):
    monkeypatch.setenv("TORCHINDUCTOR_FX_GRAPH_CACHE", "0")
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", "/somewhere/else")
    fresh_megacache.configure_env()
    assert os.environ["TORCHINDUCTOR_FX_GRAPH_CACHE"] == "0"
    assert os.environ["TORCHINDUCTOR_CACHE_DIR"] == "/somewhere/else"
    # The unset one should still pick up the default.
    assert os.environ["TORCHINDUCTOR_AUTOGRAD_CACHE"] == "1"


def test_load_artifacts_returns_false_when_file_absent(fresh_megacache):
    assert fresh_megacache.load_artifacts() is False
    assert fresh_megacache.loaded_ok() is False


def test_load_artifacts_returns_false_when_torch_missing_loader(fresh_megacache, tmp_path):
    cache_dir = tmp_path / "cache" / "inductor"
    cache_dir.mkdir(parents=True)
    (cache_dir / "megacache.bin").write_bytes(b"\x00\x01\x02")

    try:
        import torch
    except ImportError:
        pytest.skip("torch not installed")

    # If torch.compiler.load_cache_artifacts is absent (older torch),
    # load_artifacts should return False without raising.
    if hasattr(getattr(torch, "compiler", None), "load_cache_artifacts"):
        pytest.skip("torch.compiler.load_cache_artifacts is present; covered elsewhere")
    assert fresh_megacache.load_artifacts() is False


def test_save_artifacts_returns_false_when_torch_missing_saver(fresh_megacache):
    try:
        import torch
    except ImportError:
        pytest.skip("torch not installed")
    if hasattr(getattr(torch, "compiler", None), "save_cache_artifacts"):
        pytest.skip("torch.compiler.save_cache_artifacts is present; covered elsewhere")
    assert fresh_megacache.save_artifacts() is False


def test_save_artifacts_writes_file_with_stub(fresh_megacache, tmp_path, monkeypatch):
    """Drive the write path with a stubbed torch.compiler.save_cache_artifacts."""
    try:
        import torch  # noqa: F401
    except ImportError:
        pytest.skip("torch not installed")

    import torch as real_torch
    compiler_stub = type(real_torch)("compiler_stub")
    compiler_stub.save_cache_artifacts = lambda: b"hello-megacache"  # type: ignore[attr-defined]

    monkeypatch.setattr(real_torch, "compiler", compiler_stub, raising=False)

    assert fresh_megacache.save_artifacts() is True
    written = tmp_path / "cache" / "inductor" / "megacache.bin"
    assert written.is_file()
    assert written.read_bytes() == b"hello-megacache"


def test_save_artifacts_handles_tuple_return(fresh_megacache, tmp_path, monkeypatch):
    """Some PyTorch 2.7 patch levels return (bytes, info)."""
    try:
        import torch as real_torch
    except ImportError:
        pytest.skip("torch not installed")

    compiler_stub = type(real_torch)("compiler_stub")
    compiler_stub.save_cache_artifacts = lambda: (b"tuple-payload", {"keys": []})  # type: ignore[attr-defined]

    monkeypatch.setattr(real_torch, "compiler", compiler_stub, raising=False)

    assert fresh_megacache.save_artifacts() is True
    assert (tmp_path / "cache" / "inductor" / "megacache.bin").read_bytes() == b"tuple-payload"


def test_load_artifacts_marks_loaded_ok(fresh_megacache, tmp_path, monkeypatch):
    try:
        import torch as real_torch
    except ImportError:
        pytest.skip("torch not installed")

    cache_dir = tmp_path / "cache" / "inductor"
    cache_dir.mkdir(parents=True)
    (cache_dir / "megacache.bin").write_bytes(b"payload")

    received = {}

    def _loader(payload):
        received["payload"] = payload

    compiler_stub = type(real_torch)("compiler_stub")
    compiler_stub.load_cache_artifacts = _loader  # type: ignore[attr-defined]
    monkeypatch.setattr(real_torch, "compiler", compiler_stub, raising=False)

    assert fresh_megacache.load_artifacts() is True
    assert fresh_megacache.loaded_ok() is True
    assert received["payload"] == b"payload"


def test_apply_is_idempotent(fresh_megacache):
    fresh_megacache.apply()
    fresh_megacache.apply()
    # No exception, env still set, atexit not registered twice
    # (the idempotency flag prevents a second register call).
    assert fresh_megacache._APPLIED is True
    assert fresh_megacache._ATEXIT_INSTALLED is True


def test_install_atexit_is_idempotent(fresh_megacache):
    fresh_megacache.install_atexit()
    first = fresh_megacache._ATEXIT_INSTALLED
    fresh_megacache.install_atexit()
    assert first is True
    assert fresh_megacache._ATEXIT_INSTALLED is True


def test_register_capability_uses_loaded_predicate(fresh_megacache, clean_capabilities):
    fresh_megacache.register_capabilities()
    # Predicate is false initially (no load happened in this test).
    assert "runtime/megacache" not in clean_capabilities.enabled_capabilities()
    # Flip the module's load flag and re-check.
    fresh_megacache._LOADED_OK = True
    assert "runtime/megacache" in clean_capabilities.enabled_capabilities()


def test_repo_root_resolution_finds_marker():
    from modules.cplugapi import megacache

    # The resolved root must contain at least one of the markers we
    # walked up looking for. In the dev tree that's webui.py.
    assert (megacache._REPO_ROOT / "webui.py").is_file() or (
        megacache._REPO_ROOT / ".git"
    ).exists()
