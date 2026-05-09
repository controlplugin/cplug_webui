"""Tests for ``modules.cplugapi.gen_timing``.

The module wraps two upstream functions in ``modules.processing`` to
collect per-gen timing. We test the wrappers directly by stubbing
``modules.processing`` with simple callable stand-ins, then invoking
``install_hooks`` and the wrapped functions.

Coverage targets:
- One log line per gen, with structured ``total_ms`` field.
- ``decode_latent_batch`` called inside a gen contributes to
  ``vae_decode_ms``; called outside is silent.
- HR pass (decode called twice in one gen) accumulates into a single
  vae_decode_ms total.
- Idempotent install: running ``install_hooks`` twice does not
  double-wrap (call counts stay correct).
- Exceptions still surface; the timing line carries an ``error`` field.
- Capability registered.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest


@pytest.fixture
def fake_processing(monkeypatch):
    """Replace ``modules.processing`` with a stub exposing the two
    functions ``gen_timing`` wraps. Returns the stub so tests can
    re-bind the ``process_images_inner`` and ``decode_latent_batch``
    behaviour per case."""
    stub = types.ModuleType("modules.processing")

    def _default_process(p):
        return "result"

    def _default_decode(*a, **k):
        return "decoded"

    stub.process_images_inner = _default_process
    stub.decode_latent_batch = _default_decode

    monkeypatch.setitem(sys.modules, "modules.processing", stub)

    # Force a fresh hook install on this stub.
    from modules.cplugapi import gen_timing
    if hasattr(stub, gen_timing._INSTALL_FLAG):
        delattr(stub, gen_timing._INSTALL_FLAG)
    gen_timing.install_hooks()
    return stub


@pytest.fixture
def caplog_gen(caplog):
    caplog.set_level(logging.INFO, logger="cplugapi.gen_timing")
    return caplog


def _gen_records(caplog):
    return [r for r in caplog.records if r.name == "cplugapi.gen_timing"]


def test_emits_one_line_per_gen(fake_processing, caplog_gen):
    fake_processing.process_images_inner("p")
    records = _gen_records(caplog_gen)
    assert len(records) == 1
    assert records[0].total_ms >= 0
    assert "total_ms=" in records[0].getMessage()


def test_decode_outside_gen_is_silent(fake_processing, caplog_gen):
    """Bare ``decode_latent_batch`` calls (not inside process_images_inner)
    must not contribute to a phantom gen log."""
    fake_processing.decode_latent_batch("a", "b")
    assert _gen_records(caplog_gen) == []


def test_decode_inside_gen_contributes_to_vae_stage(fake_processing, caplog_gen):
    """A gen that decodes latents must surface vae_decode_ms in its line."""

    def with_decode(p):
        fake_processing.decode_latent_batch("a", "b")
        return "ok"

    fake_processing.process_images_inner = with_decode
    # Re-install since process_images_inner reference changed; but the
    # wrap captures the original at install time. Manually re-wrap for
    # this test by toggling the install flag.
    from modules.cplugapi import gen_timing as _gt
    real_proc = sys.modules["modules.processing"]
    if hasattr(real_proc, _gt._INSTALL_FLAG):
        delattr(real_proc, _gt._INSTALL_FLAG)
    _gt.install_hooks()

    real_proc.process_images_inner("p")
    records = _gen_records(caplog_gen)
    assert len(records) == 1
    assert getattr(records[0], "vae_decode_ms", -1) >= 0


def test_hr_pass_accumulates_decode_time(fake_processing, caplog_gen):
    """Two decode calls in one gen sum into one vae_decode_ms field."""

    def with_two_decodes(p):
        fake_processing.decode_latent_batch("a", "b")
        fake_processing.decode_latent_batch("c", "d")
        return "ok"

    fake_processing.process_images_inner = with_two_decodes
    from modules.cplugapi import gen_timing as _gt
    real_proc = sys.modules["modules.processing"]
    if hasattr(real_proc, _gt._INSTALL_FLAG):
        delattr(real_proc, _gt._INSTALL_FLAG)
    _gt.install_hooks()

    real_proc.process_images_inner("p")
    records = _gen_records(caplog_gen)
    assert len(records) == 1
    # Single field, summed — message has only one vae_decode_ms token.
    assert records[0].getMessage().count("vae_decode_ms=") == 1


def test_install_is_idempotent(fake_processing, caplog_gen):
    """A second install() must not double-wrap. Otherwise the log
    line would print twice and decode time would double-count."""
    from modules.cplugapi import gen_timing as _gt
    _gt.install_hooks()  # already installed in fixture; this is the 2nd
    _gt.install_hooks()  # 3rd

    fake_processing.process_images_inner("p")
    records = _gen_records(caplog_gen)
    assert len(records) == 1


def test_exception_in_gen_still_logs_with_error_field(fake_processing, caplog_gen):
    """Failed gens must surface in the log too (that's the case the
    user wants visible when diagnosing client-reported failures)."""

    class BoomError(RuntimeError):
        pass

    def boom(p):
        raise BoomError("simulated")

    fake_processing.process_images_inner = boom
    from modules.cplugapi import gen_timing as _gt
    real_proc = sys.modules["modules.processing"]
    if hasattr(real_proc, _gt._INSTALL_FLAG):
        delattr(real_proc, _gt._INSTALL_FLAG)
    _gt.install_hooks()

    with pytest.raises(BoomError, match="simulated"):
        real_proc.process_images_inner("p")
    records = _gen_records(caplog_gen)
    assert len(records) == 1
    assert getattr(records[0], "error", None) == "BoomError"
    assert "error=BoomError" in records[0].getMessage()


def test_capability_registered(progress_stub, clean_capabilities):
    """``gen-timing`` must appear in /health.capabilities so clients
    know to look for the timing log lines."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from modules.cplugapi import PREFIX, setup_cplugapi

    app = FastAPI()
    setup_cplugapi(app)
    client = TestClient(app)
    caps = client.get(f"{PREFIX}/health").json()["capabilities"]
    assert "gen-timing" in caps
