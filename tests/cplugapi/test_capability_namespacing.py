"""Tests for W15 — fork-local capability namespacing with dual emission.

Verifies:
- Both old and new strings are simultaneously registered.
- ``deprecated_capabilities()`` returns the legacy strings only.
- Canonical strings (per the project's capability registry) are NOT
  renamed.
- ``/health`` and ``/identify`` surface the deprecated list.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import (
    PREFIX,
    access_log,
    capabilities,
    gen_timing,
    livez_readyz,
    sdapi_observer,
    setup_cplugapi,
    upscale_log,
)


def _make_client():
    app = FastAPI()
    setup_cplugapi(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# register_with_legacy primitive
# ---------------------------------------------------------------------------


def test_register_with_legacy_emits_both_names(clean_capabilities):
    capabilities.register_with_legacy(new_name="ns/foo", legacy_name="foo")
    enabled = capabilities.enabled_capabilities()
    assert "ns/foo" in enabled
    assert "foo" in enabled
    assert "foo" in capabilities.deprecated_capabilities()
    assert "ns/foo" not in capabilities.deprecated_capabilities()


def test_unregister_drops_from_deprecated(clean_capabilities):
    capabilities.register_with_legacy(new_name="ns/foo", legacy_name="foo")
    assert "foo" in capabilities.deprecated_capabilities()
    capabilities.unregister("foo")
    assert "foo" not in capabilities.deprecated_capabilities()


def test_reset_clears_deprecated_set(clean_capabilities):
    capabilities.register_with_legacy(new_name="ns/foo", legacy_name="foo")
    capabilities.reset()
    assert capabilities.deprecated_capabilities() == []


# ---------------------------------------------------------------------------
# Fork-local modules emit dual names + mark legacy
# ---------------------------------------------------------------------------


def test_access_log_emits_namespaced_alias(clean_capabilities):
    access_log.register_capabilities()
    enabled = capabilities.enabled_capabilities()
    assert "observability/request-log" in enabled
    assert "request-log" in enabled
    assert "request-log" in capabilities.deprecated_capabilities()


def test_gen_timing_emits_namespaced_alias(clean_capabilities, monkeypatch):
    monkeypatch.setenv("CPLUG_GEN_TIMING", "1")
    gen_timing.register_capabilities()
    enabled = capabilities.enabled_capabilities()
    assert "observability/gen-timing" in enabled
    assert "gen-timing" in enabled
    assert "gen-timing" in capabilities.deprecated_capabilities()


def test_sdapi_observer_emits_namespaced_alias(clean_capabilities, monkeypatch):
    monkeypatch.setenv("CPLUG_SDAPI_OBSERVER", "1")
    sdapi_observer.register_capabilities()
    enabled = capabilities.enabled_capabilities()
    assert "observability/sdapi-request-log" in enabled
    assert "sdapi-request-log" in enabled
    assert "sdapi-request-log" in capabilities.deprecated_capabilities()


def test_upscale_log_emits_namespaced_alias(clean_capabilities):
    upscale_log.register_capabilities()
    enabled = capabilities.enabled_capabilities()
    assert "observability/upscale-log" in enabled
    assert "upscale-log" in enabled
    assert "upscale-log" in capabilities.deprecated_capabilities()


def test_register_with_legacy_rejects_same_name(clean_capabilities):
    """Guard against a typo / refactor accident that would make the
    same string both 'active' and 'deprecated'."""
    import pytest
    with pytest.raises(ValueError, match="must differ"):
        capabilities.register_with_legacy(
            new_name="ns/foo", legacy_name="ns/foo",
        )


def test_livez_readyz_emit_namespaced_aliases(clean_capabilities):
    livez_readyz.register_capabilities()
    enabled = capabilities.enabled_capabilities()
    assert "health/livez" in enabled
    assert "livez" in enabled
    assert "health/readyz" in enabled
    assert "readyz" in enabled
    deprecated = capabilities.deprecated_capabilities()
    assert "livez" in deprecated
    assert "readyz" in deprecated


# ---------------------------------------------------------------------------
# Canonical strings NOT renamed
# ---------------------------------------------------------------------------


def test_canonical_strings_not_deprecated(clean_capabilities):
    """The canonical registry strings (session/cancel, forge/preset,
    models/architecture, idempotency, etc.) MUST NOT be renamed — they
    are codegen-frozen on the client side. Verify none of them appear
    in deprecated_capabilities()."""
    client = _make_client()
    body = client.get(f"{PREFIX}/identify").json()
    deprecated = set(body["deprecated_capabilities"])
    canonical = {
        "identify",
        "health",
        "version",
        "idempotency",
        "queue",
        "session/cancel",
        "session/preempt",
        "forge/preset",
        "models/architecture",
        "models/disk-scan",
        "models/architectures-available",
        "controlnet/patcher-cache",
    }
    for c in canonical:
        assert c not in deprecated, f"{c!r} is canonical; must not be deprecated"


# ---------------------------------------------------------------------------
# Wire surfaces
# ---------------------------------------------------------------------------


def test_health_surfaces_deprecated_capabilities_list(clean_capabilities):
    client = _make_client()
    body = client.get(f"{PREFIX}/health").json()
    assert "deprecated_capabilities" in body
    assert isinstance(body["deprecated_capabilities"], list)
    # All deprecated strings should also be in the active capabilities
    # list (dual emission).
    for legacy in body["deprecated_capabilities"]:
        assert legacy in body["capabilities"], (
            f"{legacy!r} is deprecated but not active — should be dual-emitted"
        )


def test_identify_surfaces_deprecated_capabilities_list(clean_capabilities):
    client = _make_client()
    body = client.get(f"{PREFIX}/identify").json()
    assert "deprecated_capabilities" in body
    assert isinstance(body["deprecated_capabilities"], list)
    for legacy in body["deprecated_capabilities"]:
        assert legacy in body["capabilities"]


def test_deprecated_capabilities_sorted(clean_capabilities):
    client = _make_client()
    body = client.get(f"{PREFIX}/health").json()
    deprecated = body["deprecated_capabilities"]
    assert deprecated == sorted(deprecated)
