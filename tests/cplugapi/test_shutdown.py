"""Tests for ``modules.cplugapi.shutdown`` (W12)."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import (
    PREFIX,
    livez_readyz,
    profile,
    setup_cplugapi,
    shutdown,
)


def setup_function(_):
    livez_readyz.clear_draining()


def _run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# graceful_shutdown sequence
# ---------------------------------------------------------------------------


def test_graceful_shutdown_sets_drain_flag(clean_capabilities):
    """Drain flag flips immediately on entry."""
    assert livez_readyz.is_draining() is False
    _run_async(shutdown.graceful_shutdown(grace_s=0.01))
    assert livez_readyz.is_draining() is True


def test_graceful_shutdown_returns_early_when_no_active_work(clean_capabilities, progress_stub):
    """No current_task, no pending — sequence exits immediately."""
    progress_stub.current_task = None
    progress_stub.pending_tasks.clear()
    report = _run_async(shutdown.graceful_shutdown(grace_s=10.0, poll_interval_s=0.01))
    assert report["interrupted"] is False
    # waited_s was bounded by the early return — should be near-zero.
    assert report["waited_s"] < 0.5


def test_graceful_shutdown_waits_then_interrupts(clean_capabilities, progress_stub, shared_stub):
    """Active work + grace expires -> interrupt fires."""
    progress_stub.current_task = "stuck-task"
    report = _run_async(shutdown.graceful_shutdown(grace_s=0.1, poll_interval_s=0.05))
    assert report["interrupted"] is True
    assert shared_stub.state.interrupt_called >= 1


def test_graceful_shutdown_finishes_when_work_clears_mid_grace(clean_capabilities, progress_stub):
    """If work clears during the grace window, we exit cleanly without
    firing interrupt. Simulate by clearing mid-poll."""
    progress_stub.current_task = "almost-done"

    async def _runner():
        # Fire shutdown; it will poll. Clear the task during the wait.
        async def _clear_after():
            await asyncio.sleep(0.1)
            progress_stub.current_task = None

        await asyncio.gather(
            shutdown.graceful_shutdown(grace_s=2.0, poll_interval_s=0.05),
            _clear_after(),
        )

    _run_async(_runner())
    # Drain flag set; current_task cleared mid-grace.
    assert livez_readyz.is_draining() is True


def test_graceful_shutdown_handles_missing_modules(clean_capabilities):
    """If modules.progress is gone (early shutdown / test fixture state),
    sequence completes without raising."""
    saved = sys.modules.pop("modules.progress", None)
    try:
        report = _run_async(shutdown.graceful_shutdown(grace_s=0.01, poll_interval_s=0.005))
        assert report["interrupted"] is False
    finally:
        if saved is not None:
            sys.modules["modules.progress"] = saved


# ---------------------------------------------------------------------------
# Reject-during-drain middleware
# ---------------------------------------------------------------------------


def _make_client(install_shutdown=True):
    app = FastAPI()
    setup_cplugapi(app)
    if install_shutdown:
        shutdown.install(app)
    app.middleware_stack = app.build_middleware_stack()
    return app, TestClient(app)


def test_no_rejection_when_not_draining(clean_capabilities):
    """Drain flag off -> middleware passes everything through."""
    livez_readyz.clear_draining()
    app, client = _make_client()
    r = client.get(f"{PREFIX}/identify")
    assert r.status_code == 200


def test_no_rejection_in_desktop_default(clean_capabilities):
    """Desktop profile + drain flag on + REJECT_NEW unset -> default
    is to ACCEPT new requests during drain (don't lie to the client
    in single-replica posture)."""
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        os.environ.pop(shutdown.ENV_REJECT_NEW, None)
        app, client = _make_client()
        livez_readyz.set_draining(True)
        try:
            r = client.post(f"{PREFIX}/session/preempt")
            # Shouldn't be 503 from drain rejection; could be 200/4xx
            # from the underlying handler.
            assert r.status_code != 503 or "draining" not in r.text.lower()
        finally:
            livez_readyz.clear_draining()


def test_explicit_reject_new_rejects_post_to_cplugapi(clean_capabilities):
    """CPLUG_SHUTDOWN_REJECT_NEW=1 + drain flag on -> POST rejected
    with 503."""
    with patch.dict(os.environ, {shutdown.ENV_REJECT_NEW: "1"}):
        app, client = _make_client()
        livez_readyz.set_draining(True)
        try:
            r = client.post(f"{PREFIX}/session/preempt")
            assert r.status_code == 503
            assert r.headers.get("Retry-After") == "5"
            assert "draining" in r.json().get("detail", "").lower()
        finally:
            livez_readyz.clear_draining()


def test_reject_new_does_not_block_get(clean_capabilities):
    """Reads pass through during drain — capability/health probes
    stay reachable."""
    with patch.dict(os.environ, {shutdown.ENV_REJECT_NEW: "1"}):
        app, client = _make_client()
        livez_readyz.set_draining(True)
        try:
            r = client.get(f"{PREFIX}/identify")
            assert r.status_code == 200
        finally:
            livez_readyz.clear_draining()


def test_reject_new_targets_sdapi_gen_paths(clean_capabilities):
    """The two gen entry points (txt2img, img2img) are rejected during
    drain; other /sdapi/v1/* paths pass through."""
    with patch.dict(os.environ, {shutdown.ENV_REJECT_NEW: "1"}):
        app = FastAPI()
        setup_cplugapi(app)
        shutdown.install(app)

        @app.post("/sdapi/v1/txt2img")
        def t():
            return {"ok": True}

        @app.post("/sdapi/v1/options")
        def o():
            return {"ok": True}

        app.middleware_stack = app.build_middleware_stack()
        client = TestClient(app)

        livez_readyz.set_draining(True)
        try:
            r_gen = client.post("/sdapi/v1/txt2img")
            r_other = client.post("/sdapi/v1/options")
            assert r_gen.status_code == 503
            assert r_other.status_code == 200
        finally:
            livez_readyz.clear_draining()


def test_cloud_profile_default_rejects_new(clean_capabilities):
    """Cloud profile default flips REJECT_NEW to True."""
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}, clear=False):
        os.environ.pop(shutdown.ENV_REJECT_NEW, None)
        os.environ.pop("CPLUG_ALLOWED_HOSTS", None)
        app, client = _make_client()
        livez_readyz.set_draining(True)
        try:
            r = client.post(f"{PREFIX}/session/preempt")
            assert r.status_code == 503
        finally:
            livez_readyz.clear_draining()


# ---------------------------------------------------------------------------
# Drain flag visible on /readyz
# ---------------------------------------------------------------------------


def test_drain_flag_visible_on_readyz(clean_capabilities):
    """W12 + W1 integration: drain flag visible on the public /readyz body."""
    app, client = _make_client()
    livez_readyz.set_draining(True)
    try:
        r = client.get(f"{PREFIX}/readyz")
        assert r.status_code == 503
        body = r.json()
        assert body["checks"]["draining"] is True
    finally:
        livez_readyz.clear_draining()


# ---------------------------------------------------------------------------
# Env var resolution
# ---------------------------------------------------------------------------


def test_resolve_grace_s_default(clean_capabilities):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(shutdown.ENV_GRACE_S, None)
        assert shutdown._resolve_grace_s() == shutdown.DEFAULT_GRACE_S


def test_resolve_grace_s_explicit(clean_capabilities):
    with patch.dict(os.environ, {shutdown.ENV_GRACE_S: "5"}):
        assert shutdown._resolve_grace_s() == 5.0


def test_resolve_grace_s_invalid_falls_back(clean_capabilities):
    with patch.dict(os.environ, {shutdown.ENV_GRACE_S: "garbage"}):
        assert shutdown._resolve_grace_s() == shutdown.DEFAULT_GRACE_S


def test_resolve_reject_new_truthy_values(clean_capabilities):
    for v in ("1", "true", "yes", "on", "TRUE"):
        with patch.dict(os.environ, {shutdown.ENV_REJECT_NEW: v}):
            assert shutdown._resolve_reject_new() is True


def test_resolve_reject_new_falsy_values(clean_capabilities):
    for v in ("0", "false", "no", "off"):
        with patch.dict(
            os.environ,
            {profile.ENV_PROFILE: "cloud", shutdown.ENV_REJECT_NEW: v},
        ):
            assert shutdown._resolve_reject_new() is False


# ---------------------------------------------------------------------------
# Capability + install idempotency
# ---------------------------------------------------------------------------


def test_capability_registered(clean_capabilities):
    from modules.cplugapi import capabilities

    shutdown.register_capabilities()
    assert "ops/graceful-shutdown" in capabilities.enabled_capabilities()


def test_install_is_idempotent(clean_capabilities):
    app = FastAPI()
    setup_cplugapi(app)
    shutdown.install(app)
    n1 = len(app.user_middleware)
    shutdown.install(app)
    n2 = len(app.user_middleware)
    assert n1 == n2
