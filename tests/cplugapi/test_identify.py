"""Tests for the unauthenticated ``/cplugapi/v1/identify`` probe.

W4 surfaces ``capabilities[]`` here. The filter (`_safe_capability`)
guards against future capability strings that might accidentally
leak deployment specifics — checkpoint filenames, commit SHAs.
Today no capability triggers the filter; the tests pin the
contract so a regression is caught at CI time.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, identify, setup_cplugapi


def _make_client(auth_dependency=None):
    app = FastAPI()
    setup_cplugapi(app, auth_dependency=auth_dependency)
    return TestClient(app)


def test_identify_returns_capabilities_list(clean_capabilities):
    client = _make_client()
    body = client.get(f"{PREFIX}/identify").json()
    assert isinstance(body["capabilities"], list)
    assert all(isinstance(c, str) for c in body["capabilities"])
    # Sanity — at least the always-on capabilities are there.
    assert "identify" in body["capabilities"]
    assert "health" in body["capabilities"]


def test_identify_capabilities_surface_without_auth(clean_capabilities):
    """The whole point of W4: capability discovery without credentials."""
    from fastapi import HTTPException

    def reject_all(creds=None):
        raise HTTPException(status_code=401, detail="nope")

    client = _make_client(auth_dependency=reject_all)
    r = client.get(f"{PREFIX}/identify")
    assert r.status_code == 200
    assert isinstance(r.json()["capabilities"], list)
    assert len(r.json()["capabilities"]) > 0


def test_safe_capability_filters_hex_shas(clean_capabilities):
    """A capability string that's just a 7-40 char hex SHA must NOT
    surface on the unauthenticated probe."""
    assert identify._safe_capability("session/cancel") is True
    assert identify._safe_capability("identify") is True
    # 7-char short SHA
    assert identify._safe_capability("a1b2c3d") is False
    # 40-char full SHA
    assert identify._safe_capability("a" * 40) is False
    # Mixed case (regex is case-insensitive)
    assert identify._safe_capability("AbCdEf01234") is False


def test_safe_capability_filters_checkpoint_suffixes(clean_capabilities):
    """A capability ending in a model-file extension must NOT surface."""
    assert identify._safe_capability("models/sd_xl_base_1.0.safetensors") is False
    assert identify._safe_capability("checkpoint/foo.ckpt") is False
    assert identify._safe_capability("foo.pt") is False
    assert identify._safe_capability("foo.gguf") is False
    # Capabilities that contain a dot but NOT a checkpoint suffix are
    # rejected at registration anyway (slash-only invariant) but the
    # filter doesn't depend on that.


def test_identify_filters_unsafe_string_at_egress(clean_capabilities):
    """Defence-in-depth: even if a capability slips past the registry's
    dot-notation guard (e.g. via direct ``_registry`` injection from an
    extension or future bug), the /identify filter strips it on egress.

    The capability registry currently rejects dot-notation at
    ``register()`` time, so this test injects directly into the
    private ``_registry`` dict to prove the egress filter is the
    second line of defence and not coupled to registration validation."""
    from modules.cplugapi import capabilities as caps

    # Bypass `register()` validation by hitting the dict directly.
    bad_name = "models/sd_xl_base_1.0.safetensors"
    with caps._lock:
        caps._registry[bad_name] = lambda: True
    try:
        # Confirm the registry surfaces the bad name (no validation here).
        assert bad_name in caps.enabled_capabilities()
        # /identify must filter it out.
        client = _make_client()
        body = client.get(f"{PREFIX}/identify").json()
        assert bad_name not in body["capabilities"]
    finally:
        with caps._lock:
            caps._registry.pop(bad_name, None)


def test_identify_capabilities_sorted(clean_capabilities):
    """Sorted output is stable for diffing across runs."""
    client = _make_client()
    body = client.get(f"{PREFIX}/identify").json()
    assert body["capabilities"] == sorted(body["capabilities"])
