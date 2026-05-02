"""Tests for ``modules.cplugapi.idempotency`` middleware."""

from __future__ import annotations

import os
from unittest.mock import patch

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, idempotency, setup_cplugapi


def _make_client_with(extra_attach):
    """Mount cplugapi + idempotency middleware + per-test routes."""
    app = FastAPI()
    setup_cplugapi(app)
    idempotency.install(app)
    extra = APIRouter()
    extra_attach(extra)
    app.include_router(extra, prefix=PREFIX)
    return TestClient(app)


def _counting_post_attach(call_log: list, fail_first: bool = False):
    """Returns an attach function that registers POST / GET probes
    incrementing ``call_log`` so tests can assert the route fired."""

    def attach(r: APIRouter) -> None:
        @r.post("/_idem_probe")
        def post_probe() -> dict:
            call_log.append("post")
            if fail_first and len(call_log) == 1:
                from fastapi import HTTPException

                raise HTTPException(status_code=422, detail="bad")
            return {"n": len(call_log), "method": "post"}

        @r.get("/_idem_probe")
        def get_probe() -> dict:
            call_log.append("get")
            return {"n": len(call_log), "method": "get"}

    return attach


def setup_function(_):
    """Fresh cache for every test — bypasses cross-test bleed."""
    idempotency.reset_cache()


def test_first_call_invokes_route_and_caches(clean_capabilities):
    calls: list[str] = []
    client = _make_client_with(_counting_post_attach(calls))

    r = client.post(
        f"{PREFIX}/_idem_probe", headers={"Idempotency-Key": "key-abc-12345"}
    )
    assert r.status_code == 200
    assert r.json() == {"n": 1, "method": "post"}
    # First call has no replay marker.
    assert r.headers.get("Idempotency-Replayed") != "true"
    assert calls == ["post"]


def test_second_call_replays_cached_response(clean_capabilities):
    calls: list[str] = []
    client = _make_client_with(_counting_post_attach(calls))

    headers = {"Idempotency-Key": "key-abc-12345"}
    first = client.post(f"{PREFIX}/_idem_probe", headers=headers)
    second = client.post(f"{PREFIX}/_idem_probe", headers=headers)

    assert second.status_code == first.status_code
    assert second.json() == first.json()
    assert second.headers.get("Idempotency-Replayed") == "true"
    # Route only invoked once even though we called it twice.
    assert calls == ["post"]


def test_4xx_response_is_cached_too(clean_capabilities):
    """A retry after a validation error must replay the same error,
    not slip past it because the body changed mid-flight."""
    calls: list[str] = []
    client = _make_client_with(_counting_post_attach(calls, fail_first=True))

    headers = {"Idempotency-Key": "key-validation-err-1"}
    first = client.post(f"{PREFIX}/_idem_probe", headers=headers)
    assert first.status_code == 422

    second = client.post(f"{PREFIX}/_idem_probe", headers=headers)
    assert second.status_code == 422
    assert second.headers.get("Idempotency-Replayed") == "true"
    # Route fired exactly once.
    assert calls == ["post"]


def test_malformed_key_rejected_with_400(clean_capabilities):
    calls: list[str] = []
    client = _make_client_with(_counting_post_attach(calls))

    bad_keys = [
        "short",  # < 8 chars
        "a" * 200,  # > 128 chars
        "has spaces inside",  # disallowed char
        "tab\tinside-key",  # control char
        "newline\ninside",  # control char
    ]
    for bad in bad_keys:
        r = client.post(
            f"{PREFIX}/_idem_probe", headers={"Idempotency-Key": bad}
        )
        assert r.status_code == 400, bad
        body = r.json()
        assert body.get("error") == "invalid_idempotency_key"

    # None of the malformed calls reached the handler.
    assert calls == []


def test_get_method_passes_through(clean_capabilities):
    """GET / HEAD / OPTIONS are non-mutating — middleware must not
    cache them even if a key is supplied."""
    calls: list[str] = []
    client = _make_client_with(_counting_post_attach(calls))

    headers = {"Idempotency-Key": "key-get-passthrough-1"}
    r1 = client.get(f"{PREFIX}/_idem_probe", headers=headers)
    r2 = client.get(f"{PREFIX}/_idem_probe", headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Handler invoked twice — no caching.
    assert calls == ["get", "get"]
    assert r2.headers.get("Idempotency-Replayed") != "true"


def test_no_header_passes_through(clean_capabilities):
    """Without a key, middleware is a pure pass-through."""
    calls: list[str] = []
    client = _make_client_with(_counting_post_attach(calls))

    r1 = client.post(f"{PREFIX}/_idem_probe")
    r2 = client.post(f"{PREFIX}/_idem_probe")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls == ["post", "post"]
    assert r1.json()["n"] == 1
    assert r2.json()["n"] == 2


def test_outside_cplugapi_prefix_passes_through(clean_capabilities):
    """Requests outside ``/cplugapi/v1/*`` must not interact with the
    cache — preserves /sdapi byte-identity."""
    app = FastAPI()
    setup_cplugapi(app)
    idempotency.install(app)

    counter = {"n": 0}

    @app.post("/sdapi/fake")
    def fake() -> dict:
        counter["n"] += 1
        return {"n": counter["n"]}

    client = TestClient(app)
    headers = {"Idempotency-Key": "key-sdapi-passthrough-1"}
    a = client.post("/sdapi/fake", headers=headers)
    b = client.post("/sdapi/fake", headers=headers)
    assert a.json() == {"n": 1}
    assert b.json() == {"n": 2}  # not replayed
    assert "Idempotency-Replayed" not in b.headers


def test_env_var_max_cap_honored(clean_capabilities):
    """``CPLUG_IDEMPOTENCY_MAX`` shrinks the LRU cap; oldest evicted."""
    calls: list[str] = []
    with patch.dict(os.environ, {"CPLUG_IDEMPOTENCY_MAX": "2"}):
        client = _make_client_with(_counting_post_attach(calls))

        for i in range(3):
            client.post(
                f"{PREFIX}/_idem_probe",
                headers={"Idempotency-Key": f"key-0000000{i}"},
            )

        assert idempotency.cache_size() == 2

        # The oldest key (idx 0) was evicted, so re-using it triggers
        # the handler again.
        before = len(calls)
        r = client.post(
            f"{PREFIX}/_idem_probe",
            headers={"Idempotency-Key": "key-00000000"},
        )
        assert r.status_code == 200
        assert r.headers.get("Idempotency-Replayed") != "true"
        assert len(calls) == before + 1


def test_env_var_ttl_expiry(clean_capabilities):
    """A cache entry past its TTL is treated as a miss."""
    import time as real_time

    calls: list[str] = []
    with patch.dict(os.environ, {"CPLUG_IDEMPOTENCY_TTL_S": "1"}):
        client = _make_client_with(_counting_post_attach(calls))

        headers = {"Idempotency-Key": "key-ttl-test-001"}
        client.post(f"{PREFIX}/_idem_probe", headers=headers)
        assert calls == ["post"]

        # Advance monotonic forward by far more than the 1 s TTL — pin
        # the clock at "real now + 10 minutes" so the cached entry is
        # comfortably past expiry.
        future = real_time.monotonic() + 600.0
        with patch.object(
            idempotency.time, "monotonic", side_effect=lambda: future
        ):
            r = client.post(f"{PREFIX}/_idem_probe", headers=headers)
        assert r.headers.get("Idempotency-Replayed") != "true"
        assert calls == ["post", "post"]


def test_different_paths_have_different_cache_entries(clean_capabilities):
    """``(method, path, key)`` is the cache key — same key on different
    paths must not collide."""
    calls: list[str] = []

    def attach(r: APIRouter) -> None:
        @r.post("/_idem_a")
        def a() -> dict:
            calls.append("a")
            return {"who": "a"}

        @r.post("/_idem_b")
        def b() -> dict:
            calls.append("b")
            return {"who": "b"}

    client = _make_client_with(attach)
    headers = {"Idempotency-Key": "key-shared-by-paths-1"}
    a1 = client.post(f"{PREFIX}/_idem_a", headers=headers)
    b1 = client.post(f"{PREFIX}/_idem_b", headers=headers)

    assert a1.json() == {"who": "a"}
    assert b1.json() == {"who": "b"}
    assert calls == ["a", "b"]


def test_install_is_idempotent(clean_capabilities):
    app = FastAPI()
    setup_cplugapi(app)
    idempotency.install(app)
    n1 = len(app.user_middleware)
    idempotency.install(app)
    n2 = len(app.user_middleware)
    assert n1 == n2


def test_register_capabilities_adds_idempotency(clean_capabilities):
    from modules.cplugapi import capabilities

    idempotency.register_capabilities()
    assert "idempotency" in capabilities.enabled_capabilities()
