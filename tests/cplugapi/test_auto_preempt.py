"""Tests for ``modules.cplugapi.auto_preempt``.

The middleware fires ``shared.state.interrupt()`` and drains the
pending queue before forwarding incoming gen requests. Behavior is
mode-driven (``CPLUG_PREEMPT_MODE`` env var). Tests exercise:

- ``always`` mode (fork default): every gen request preempts.
- ``header`` mode: only when ``X-Cplug-Preempt: 1`` is present.
- ``off`` mode: pure passthrough, never preempts.
- Pre-handler ordering: interrupt fires + cancelled_tasks gets the
  running id BEFORE the upstream handler runs.
- Pending queue drained on preempt (regardless of mode that triggered).
- Non-gen paths (e.g. /sdapi/v1/options) untouched.
- Capability registration reflects the active mode.
- Mode resolver tolerates garbage env values (falls back to default).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import (
    PREFIX,
    auto_preempt,
    cancelled_tasks,
    setup_cplugapi,
)
from modules.cplugapi.auto_preempt import (
    DEFAULT_MODE,
    ENV_MODE,
    MODE_ALWAYS,
    MODE_HEADER,
    MODE_OFF,
)


@pytest.fixture(autouse=True)
def _reset_cancelled():
    cancelled_tasks.reset()
    yield
    cancelled_tasks.reset()


def _build_app_with_gen_routes(mode):
    """Construct a fresh app where the cplugapi mount runs auto-preempt
    in the requested mode, plus stand-in /sdapi/v1/{txt2img,img2img}
    routes that just record they were hit (we can't actually run Forge
    in tests)."""
    import os
    os.environ[ENV_MODE] = mode
    try:
        app = FastAPI()
        setup_cplugapi(app)

        hit_log: list[str] = []
        app.state.hit_log = hit_log

        @app.post("/sdapi/v1/txt2img")
        def txt2img():
            hit_log.append("txt2img")
            return {"images": []}

        @app.post("/sdapi/v1/img2img")
        def img2img():
            hit_log.append("img2img")
            return {"images": []}

        @app.post("/sdapi/v1/options")
        def options():
            hit_log.append("options")
            return {"ok": True}

        return app
    finally:
        os.environ.pop(ENV_MODE, None)


def test_always_mode_preempts_every_gen(progress_stub, shared_stub, clean_capabilities):
    """Fork default: every gen request preempts the running task,
    even without any header."""
    progress_stub.current_task = "task(running-1)"

    app = _build_app_with_gen_routes(MODE_ALWAYS)
    client = TestClient(app)
    r = client.post("/sdapi/v1/img2img", json={})
    assert r.status_code == 200

    # interrupt fired exactly once on the global state.
    assert shared_stub.state.interrupt_called == 1
    # The running task is now in the cancelled registry — late status
    # pokes will see "already_cancelled" not "not_found".
    assert cancelled_tasks.has("task(running-1)")
    # Handler still ran — preempt is pre-handler, not blocking.
    assert app.state.hit_log == ["img2img"]


def test_always_mode_drains_pending_queue(progress_stub, shared_stub, clean_capabilities):
    """Drain prevents older queued gens from running after the new
    one arrives. Without the drain, the new gen would lose the
    queue_lock race to the older queued gens."""
    progress_stub.current_task = "task(running)"
    progress_stub.pending_tasks["task(p1)"] = 1.0
    progress_stub.pending_tasks["task(p2)"] = 2.0

    app = _build_app_with_gen_routes(MODE_ALWAYS)
    client = TestClient(app)
    client.post("/sdapi/v1/txt2img", json={})

    assert len(progress_stub.pending_tasks) == 0
    for tid in ("task(running)", "task(p1)", "task(p2)"):
        assert cancelled_tasks.has(tid)


def test_always_mode_idle_state_is_clean_passthrough(progress_stub, shared_stub, clean_capabilities):
    """No running task, no queue → preempt is a no-op. Nothing to
    interrupt, nothing to clear, no log noise."""
    app = _build_app_with_gen_routes(MODE_ALWAYS)
    client = TestClient(app)
    r = client.post("/sdapi/v1/img2img", json={})
    assert r.status_code == 200
    assert shared_stub.state.interrupt_called == 0


def test_header_mode_requires_explicit_optin(progress_stub, shared_stub, clean_capabilities):
    """In header mode, a request without the header must NOT preempt —
    that's the whole point of opting in per request."""
    progress_stub.current_task = "task(running)"

    app = _build_app_with_gen_routes(MODE_HEADER)
    client = TestClient(app)
    client.post("/sdapi/v1/img2img", json={})  # no header
    assert shared_stub.state.interrupt_called == 0
    assert not cancelled_tasks.has("task(running)")


def test_header_mode_fires_when_header_truthy(progress_stub, shared_stub, clean_capabilities):
    progress_stub.current_task = "task(running)"

    app = _build_app_with_gen_routes(MODE_HEADER)
    client = TestClient(app)
    client.post(
        "/sdapi/v1/img2img",
        json={},
        headers={"X-Cplug-Preempt": "1"},
    )
    assert shared_stub.state.interrupt_called == 1
    assert cancelled_tasks.has("task(running)")


@pytest.mark.parametrize("value", ["1", "true", "True", "YES", "on"])
def test_header_truthy_variants(progress_stub, shared_stub, clean_capabilities, value):
    """The client may stringify booleans differently — accept the
    common spellings rather than being strict."""
    progress_stub.current_task = "running"
    app = _build_app_with_gen_routes(MODE_HEADER)
    TestClient(app).post(
        "/sdapi/v1/txt2img",
        json={},
        headers={"X-Cplug-Preempt": value},
    )
    assert shared_stub.state.interrupt_called == 1


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_header_falsey_variants_do_not_preempt(progress_stub, shared_stub, clean_capabilities, value):
    progress_stub.current_task = "running"
    app = _build_app_with_gen_routes(MODE_HEADER)
    TestClient(app).post(
        "/sdapi/v1/txt2img",
        json={},
        headers={"X-Cplug-Preempt": value},
    )
    assert shared_stub.state.interrupt_called == 0


def test_off_mode_never_preempts(progress_stub, shared_stub, clean_capabilities):
    """Off mode is pure passthrough — even an explicit header is
    ignored (header is only meaningful in header mode)."""
    progress_stub.current_task = "task(running)"

    app = _build_app_with_gen_routes(MODE_OFF)
    client = TestClient(app)
    client.post(
        "/sdapi/v1/img2img",
        json={},
        headers={"X-Cplug-Preempt": "1"},
    )
    assert shared_stub.state.interrupt_called == 0


def test_non_gen_paths_untouched(progress_stub, shared_stub, clean_capabilities):
    """``/sdapi/v1/options`` doesn't trigger gens — preempting on it
    would be ill-defined, so the middleware must skip it even in
    always mode."""
    progress_stub.current_task = "task(running)"

    app = _build_app_with_gen_routes(MODE_ALWAYS)
    client = TestClient(app)
    client.post("/sdapi/v1/options", json={})
    assert shared_stub.state.interrupt_called == 0


def test_default_mode_is_always(monkeypatch):
    """Fork-specific default: when the env var isn't set,
    ``always`` is what we want — sketch workflows are the norm."""
    monkeypatch.delenv(ENV_MODE, raising=False)
    assert auto_preempt._resolve_mode() == DEFAULT_MODE
    assert DEFAULT_MODE == MODE_ALWAYS


def test_invalid_mode_falls_back_to_default(monkeypatch, caplog):
    """A typo in the env var must not silently produce passthrough —
    log a warning and fall back to the documented default so the
    operator sees the misconfiguration."""
    import logging
    monkeypatch.setenv(ENV_MODE, "agressive")  # typo
    caplog.set_level(logging.WARNING, logger="cplugapi.preempt")
    assert auto_preempt._resolve_mode() == DEFAULT_MODE


def test_capability_advertises_active_mode(progress_stub, clean_capabilities):
    """Clients can detect the active mode via /health.capabilities
    without a separate config endpoint round-trip. ``always`` build
    advertises both ``sdapi/preempt`` and ``sdapi/preempt-always``."""
    app = _build_app_with_gen_routes(MODE_ALWAYS)
    caps = TestClient(app).get(f"{PREFIX}/health").json()["capabilities"]
    assert "sdapi/preempt" in caps
    assert "sdapi/preempt-always" in caps
    assert "sdapi/preempt-header" not in caps


def test_off_mode_advertises_no_preempt_capability(progress_stub, clean_capabilities):
    """Off mode is upstream-equivalent — clients shouldn't see
    preempt advertised."""
    app = _build_app_with_gen_routes(MODE_OFF)
    caps = TestClient(app).get(f"{PREFIX}/health").json()["capabilities"]
    assert not any(c.startswith("sdapi/preempt") for c in caps)
