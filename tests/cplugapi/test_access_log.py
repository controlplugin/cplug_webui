"""Tests for ``modules.cplugapi.access_log``.

The middleware must:
- Emit one log line per ``/cplugapi/v1/*`` request, with timing.
- Stay silent outside the prefix (so ``/sdapi/v1/*`` is byte-identical
  with upstream and unrelated routes don't pollute the log).
- Honour the ``CPLUG_ACCESS_LOG=0`` kill switch.
- Surface idempotency replays via the ``replayed`` field so cached
  responses are distinguishable from real handler executions.
- Survive handler exceptions cleanly (log the failure, re-raise).
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, access_log, setup_cplugapi


@pytest.fixture
def caplog_access(caplog):
    """Capture only the cplugapi.access logger at INFO level.

    Forge's ``setup_logger`` sets ``propagate=False`` on cplugapi
    loggers (so production output goes through the Rich console
    handler, not the root). Pytest's caplog attaches to root, so we
    flip propagate back on for the test."""
    logger = logging.getLogger("cplugapi.access")
    original = logger.propagate
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="cplugapi.access")
    try:
        yield caplog
    finally:
        logger.propagate = original


def _make_client():
    app = FastAPI()
    setup_cplugapi(app)
    return TestClient(app)


def test_emits_one_line_per_request(caplog_access, progress_stub, clean_capabilities):
    client = _make_client()
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    records = [r for r in caplog_access.records if r.name == "cplugapi.access"]
    assert len(records) == 1
    record = records[0]
    assert record.method == "GET"
    assert record.path == f"{PREFIX}/health"
    assert record.status == 200
    assert record.dur_ms >= 0
    # Must surface request_id so client + server logs can be joined.
    assert record.request_id is not None and record.request_id.startswith("req_")


def test_silent_outside_prefix(caplog_access, progress_stub, clean_capabilities):
    """An app that mounts cplugapi must NOT log non-cplugapi requests."""
    app = FastAPI()
    setup_cplugapi(app)

    @app.get("/some-other-route")
    def other():
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/some-other-route")
    assert r.status_code == 200
    records = [r for r in caplog_access.records if r.name == "cplugapi.access"]
    assert records == []


def test_kill_switch_disables_emission(monkeypatch, caplog_access, progress_stub, clean_capabilities):
    """``CPLUG_ACCESS_LOG=0`` must keep the middleware silent.

    Read-once-at-install means the env var has to be set BEFORE the
    middleware is constructed for the test to be meaningful — we set
    it then build a fresh app.
    """
    monkeypatch.setenv("CPLUG_ACCESS_LOG", "0")
    app = FastAPI()
    setup_cplugapi(app)
    client = TestClient(app)

    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    records = [r for r in caplog_access.records if r.name == "cplugapi.access"]
    assert records == []


def test_logs_4xx_status(caplog_access, progress_stub, clean_capabilities):
    """Error responses must show up in the log too — that's exactly the
    case the user wants to see when diagnosing client-reported failures."""
    client = _make_client()
    r = client.post(f"{PREFIX}/forge/preset/__nope__")
    assert r.status_code == 404
    records = [r for r in caplog_access.records if r.name == "cplugapi.access"]
    assert any(rec.status == 404 for rec in records)


def test_idempotency_replay_marked(caplog_access, progress_stub, clean_capabilities):
    """A cached idempotent replay must be distinguishable in the log.

    Otherwise repeated client retries look like real handler executions
    and skew any timing analysis."""
    from modules.cplugapi import idempotency
    idempotency.reset_cache()

    client = _make_client()
    headers = {"Idempotency-Key": "abcdefgh-replay-test"}
    # First call populates the cache.
    r1 = client.post(f"{PREFIX}/forge/preset/default", headers=headers)
    assert r1.status_code == 200
    # Second call replays.
    r2 = client.post(f"{PREFIX}/forge/preset/default", headers=headers)
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"

    records = [r for r in caplog_access.records if r.name == "cplugapi.access"]
    # Both calls logged.
    assert len(records) >= 2
    replayed_flags = [getattr(r, "replayed", False) for r in records]
    # Exactly one of the two should be flagged as a replay.
    assert replayed_flags.count(True) == 1


def test_capability_registered(progress_stub, clean_capabilities):
    """``request-log`` must appear in /health.capabilities so clients
    can advertise that their server emits machine-readable access lines."""
    client = _make_client()
    caps = client.get(f"{PREFIX}/health").json()["capabilities"]
    assert "request-log" in caps


def test_format_is_grep_friendly(caplog_access, progress_stub, clean_capabilities):
    """Rendered message must contain the structured fields in stable
    key=value form so an operator can grep without parsing JSON."""
    client = _make_client()
    client.get(f"{PREFIX}/health")
    records = [r for r in caplog_access.records if r.name == "cplugapi.access"]
    msg = records[0].getMessage()
    assert "GET" in msg and f"{PREFIX}/health" in msg
    assert "status=200" in msg
    assert "dur_ms=" in msg
    assert "req_id=req_" in msg
