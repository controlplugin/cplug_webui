"""Unit tests for ``modules.cplugapi.capabilities``."""

from __future__ import annotations

import threading

import pytest

from modules.cplugapi import capabilities


def test_register_simple_string_appears_enabled(clean_capabilities):
    capabilities.register("identify")
    assert "identify" in capabilities.enabled_capabilities()


def test_enabled_returns_sorted(clean_capabilities):
    for name in ("session/cancel", "identify", "health", "version"):
        capabilities.register(name)
    assert capabilities.enabled_capabilities() == [
        "health",
        "identify",
        "session/cancel",
        "version",
    ]


def test_register_with_predicate_filters(clean_capabilities):
    flag = {"on": True}
    capabilities.register("canvas/strokes", predicate=lambda: flag["on"])
    assert "canvas/strokes" in capabilities.enabled_capabilities()
    flag["on"] = False
    assert "canvas/strokes" not in capabilities.enabled_capabilities()


def test_dot_notation_rejected(clean_capabilities):
    with pytest.raises(ValueError, match="dot notation"):
        capabilities.register("transport.base64")


def test_empty_or_whitespace_name_rejected(clean_capabilities):
    with pytest.raises(ValueError):
        capabilities.register("")
    with pytest.raises(ValueError):
        capabilities.register("  identify  ")


def test_register_is_idempotent(clean_capabilities):
    capabilities.register("health")
    capabilities.register("health")
    capabilities.register("health", predicate=lambda: False)
    assert "health" not in capabilities.enabled_capabilities()


def test_predicate_exception_silently_drops(clean_capabilities):
    def boom():
        raise RuntimeError("oops")

    capabilities.register("ok", predicate=lambda: True)
    capabilities.register("broken", predicate=boom)
    enabled = capabilities.enabled_capabilities()
    assert "ok" in enabled
    assert "broken" not in enabled


def test_unregister_removes(clean_capabilities):
    capabilities.register("identify")
    capabilities.unregister("identify")
    assert capabilities.enabled_capabilities() == []
    # Idempotent — second remove is a no-op.
    capabilities.unregister("identify")


def test_register_is_thread_safe(clean_capabilities):
    """200 threads each register a unique cap; all must end up in the registry."""

    def worker(i):
        capabilities.register(f"area/feature-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    enabled = capabilities.enabled_capabilities()
    assert len(enabled) == 200
    assert all(f"area/feature-{i}" in enabled for i in range(200))


def test_model_arch_detection_capabilities_in_health(progress_stub, clean_capabilities):
    """Model arch detection caps appear in /health response."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from modules.cplugapi import PREFIX, setup_cplugapi

    app = FastAPI()
    setup_cplugapi(app)
    client = TestClient(app)
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    caps = r.json()["capabilities"]
    assert "models/architecture" in caps
    assert "models/disk-scan" in caps
    assert "models/architectures-available" in caps
