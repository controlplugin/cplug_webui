"""Tests for ``modules.cplugapi.profile`` and the profile-driven
defaults that ``security_middleware`` and ``auto_preempt`` consult."""

from __future__ import annotations

import os
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import (
    PREFIX,
    auto_preempt,
    profile,
    security_middleware,
    setup_cplugapi,
)


# ---------------------------------------------------------------------------
# profile.get_profile()
# ---------------------------------------------------------------------------


def test_default_profile_is_desktop():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(profile.ENV_PROFILE, None)
        assert profile.get_profile() == profile.PROFILE_DESKTOP
        assert profile.is_desktop() is True
        assert profile.is_cloud() is False


def test_explicit_cloud_profile():
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}):
        assert profile.get_profile() == profile.PROFILE_CLOUD
        assert profile.is_cloud() is True
        assert profile.is_desktop() is False


def test_explicit_desktop_profile():
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}):
        assert profile.get_profile() == profile.PROFILE_DESKTOP


def test_unknown_profile_falls_back_to_default():
    with patch.dict(os.environ, {profile.ENV_PROFILE: "kubernetes"}):
        assert profile.get_profile() == profile.PROFILE_DESKTOP


def test_capability_registers_only_when_cloud(clean_capabilities):
    from modules.cplugapi import capabilities as caps

    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}):
        profile.register_capabilities()
        assert "deployment-profile-cloud" not in caps.enabled_capabilities()

    caps.reset()
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}):
        profile.register_capabilities()
        assert "deployment-profile-cloud" in caps.enabled_capabilities()


# ---------------------------------------------------------------------------
# security_middleware: profile-driven Host / Origin defaults
# ---------------------------------------------------------------------------


def test_security_cloud_profile_accepts_any_host(clean_capabilities):
    """Cloud profile -> wildcard Host allow-list. The ingress controls
    vhost routing; rebind defence at our layer is redundant."""
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}, clear=False):
        # Clear explicit override that the test bootstrap sets.
        os.environ.pop(security_middleware.ENV_ALLOWED_HOSTS, None)
        app = FastAPI()
        setup_cplugapi(app)
        client = TestClient(app)
        r = client.get(f"{PREFIX}/identify", headers={"Host": "evil.example"})
        # Wildcard allows the host through; identify returns 200.
        assert r.status_code == 200


def test_security_desktop_profile_rejects_non_loopback_host(clean_capabilities):
    """Desktop profile (default) rejects non-loopback hosts."""
    # Drop the test fixture's testserver allow-list to exercise the
    # default behaviour.
    saved = os.environ.pop(security_middleware.ENV_ALLOWED_HOSTS, None)
    try:
        with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
            os.environ.pop(profile.ENV_PROFILE, None)
            os.environ[profile.ENV_PROFILE] = "desktop"
            app = FastAPI()
            setup_cplugapi(app)
            client = TestClient(app)
            r = client.get(f"{PREFIX}/identify", headers={"Host": "evil.example"})
            assert r.status_code == 403
            assert r.json()["code"] == "host_not_allowed"
    finally:
        if saved is not None:
            os.environ[security_middleware.ENV_ALLOWED_HOSTS] = saved


def test_explicit_env_overrides_cloud_profile(clean_capabilities):
    """Explicit CPLUG_ALLOWED_HOSTS wins over the profile default."""
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            security_middleware.ENV_ALLOWED_HOSTS: "api.example.com",
        },
    ):
        app = FastAPI()
        setup_cplugapi(app)
        client = TestClient(app)
        r = client.get(f"{PREFIX}/identify", headers={"Host": "evil.example"})
        # Only api.example.com is allowed; evil.example is rejected.
        assert r.status_code == 403


def test_security_cloud_profile_accepts_any_origin(clean_capabilities):
    """Cloud profile -> wildcard Origin. Sec-Fetch-Site is the actual
    cross-origin gate."""
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud"},
        clear=False,
    ):
        os.environ.pop(security_middleware.ENV_ALLOWED_ORIGINS, None)
        app = FastAPI()
        setup_cplugapi(app)
        client = TestClient(app)
        r = client.get(
            f"{PREFIX}/identify",
            headers={"Origin": "https://app.example.com"},
        )
        assert r.status_code == 200


def test_security_cloud_still_rejects_cross_site_sec_fetch(clean_capabilities):
    """Wildcard Origin doesn't disable Sec-Fetch-Site checks."""
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud"},
        clear=False,
    ):
        os.environ.pop(security_middleware.ENV_ALLOWED_ORIGINS, None)
        app = FastAPI()
        setup_cplugapi(app)
        client = TestClient(app)
        r = client.get(
            f"{PREFIX}/identify",
            headers={
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert r.status_code == 403
        assert r.json()["code"] == "sec_fetch_site_not_allowed"


# ---------------------------------------------------------------------------
# auto_preempt: profile-driven mode default
# ---------------------------------------------------------------------------


def test_auto_preempt_default_mode_desktop_is_always():
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        os.environ.pop(auto_preempt.ENV_MODE, None)
        assert auto_preempt._resolve_mode() == auto_preempt.MODE_ALWAYS


def test_auto_preempt_default_mode_cloud_is_off():
    """Cloud is not the sketch-workflow target; preempt-by-default is
    surprising. Operators opt in explicitly."""
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}, clear=False):
        os.environ.pop(auto_preempt.ENV_MODE, None)
        assert auto_preempt._resolve_mode() == auto_preempt.MODE_OFF


def test_auto_preempt_explicit_env_overrides_cloud_default():
    """CPLUG_PREEMPT_MODE wins regardless of profile."""
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud", auto_preempt.ENV_MODE: "always"},
    ):
        assert auto_preempt._resolve_mode() == auto_preempt.MODE_ALWAYS


# ---------------------------------------------------------------------------
# /identify surfaces deployment-profile-cloud capability
# ---------------------------------------------------------------------------


def test_identify_surfaces_cloud_profile_capability(clean_capabilities):
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud"},
        clear=False,
    ):
        os.environ.pop(security_middleware.ENV_ALLOWED_HOSTS, None)
        app = FastAPI()
        setup_cplugapi(app)
        client = TestClient(app)
        body = client.get(f"{PREFIX}/identify").json()
        assert "deployment-profile-cloud" in body["capabilities"]


def test_identify_omits_profile_capability_in_desktop(clean_capabilities):
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        app = FastAPI()
        setup_cplugapi(app)
        client = TestClient(app)
        body = client.get(f"{PREFIX}/identify").json()
        assert "deployment-profile-cloud" not in body["capabilities"]
