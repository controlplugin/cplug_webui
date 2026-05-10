"""Tests for ``modules.cplugapi.rate_limit`` (W8).

Token-bucket math, profile-driven defaults, 429 envelope shape,
header emission, auth-dep wrap counting, XFF / trusted-proxies
parsing, fail-fast startup validation.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from modules.cplugapi import PREFIX, profile, rate_limit, setup_cplugapi
from modules.cplugapi.errors import CODES, PROBLEM_JSON


def setup_function(_):
    rate_limit.reset_for_test()


# ---------------------------------------------------------------------------
# Bucket math
# ---------------------------------------------------------------------------


def test_bucket_starts_full(clean_capabilities):
    b = rate_limit._Bucket(capacity=5, now=0.0)
    assert b.tokens == 5.0


def test_bucket_consumes_one_per_take(clean_capabilities):
    """Regression: pre-WB-scrutiny ``_Bucket.take`` returned a confused
    ``allowed`` value (always True) which was masked by a separate
    check at the ``_ClassRegistry.take`` layer. The fix makes
    ``_Bucket.take``'s ``consumed`` correctly track whether a token
    was taken. Verify all four positions of the truth table."""
    b = rate_limit._Bucket(capacity=3, now=0.0)
    consumed_1, _, _, _ = b.take(now=0.0)
    consumed_2, _, _, _ = b.take(now=0.0)
    consumed_3, _, _, _ = b.take(now=0.0)
    consumed_4, _, _, _ = b.take(now=0.0)
    assert consumed_1 and consumed_2 and consumed_3
    # 4th take has no tokens — MUST report not consumed.
    assert consumed_4 is False
    assert b.tokens < 1.0


def test_bucket_retry_at_reflects_time_to_one_token_not_full(clean_capabilities):
    """Regression: pre-final-scrutiny middleware used time-to-full-refill
    for the Retry-After header, which over-throttled high-capacity
    classes (600/min read class -> 60s Retry-After when 0.1s would
    have sufficed). After the fix, Retry-After uses time-to-one-token."""
    # cap=600 -> 10 tokens/sec. Empty bucket should refill 1 token in 0.1s.
    b = rate_limit._Bucket(capacity=600, now=0.0)
    # Drain.
    for _ in range(600):
        b.take(now=0.0)
    # Now empty. take() reports retry_at = now + 0.1s, reset_at = now + 60s.
    consumed, _, reset_at, retry_at = b.take(now=0.0)
    assert consumed is False
    # Reset_at: full refill (60s).
    assert 59.0 <= (reset_at - 0.0) <= 61.0
    # Retry_at: one more token (0.1s) — orders of magnitude shorter.
    assert 0.05 <= (retry_at - 0.0) <= 0.2


def test_bucket_retry_at_equals_now_when_tokens_available(clean_capabilities):
    """Take from a non-empty bucket -> retry_at is now (next take is
    available immediately)."""
    b = rate_limit._Bucket(capacity=10, now=0.0)
    consumed, _, _, retry_at = b.take(now=0.0)
    assert consumed is True
    # 9 tokens remain; next take has tokens >= 1.0 so retry-to-one is 0.
    assert retry_at == 0.0


def test_bucket_peek_does_not_consume(clean_capabilities):
    """Peek must NOT decrement the bucket — it's the cheap pre-check
    used by the auth-failure wrap to short-circuit when empty."""
    b = rate_limit._Bucket(capacity=2, now=0.0)
    assert b.peek(now=0.0) is True
    assert b.tokens == 2.0  # unchanged
    b.take(now=0.0)  # 2 -> 1
    b.take(now=0.0)  # 1 -> 0
    assert b.peek(now=0.0) is False
    assert b.tokens < 1.0
    # Even repeated peek-when-empty doesn't drive tokens negative.
    for _ in range(5):
        assert b.peek(now=0.0) is False
    assert b.tokens >= 0


def test_bucket_refills_continuously(clean_capabilities):
    """30 req/min = 0.5 tokens/sec; after 2 sec, 1 token regenerated."""
    b = rate_limit._Bucket(capacity=30, now=0.0)
    for _ in range(30):
        b.take(now=0.0)
    assert b.tokens < 1.0
    # 2 seconds later, ~1 token should have refilled.
    b._refill(now=2.0)
    assert 0.9 <= b.tokens <= 1.1


# ---------------------------------------------------------------------------
# Class registry — profile-driven defaults
# ---------------------------------------------------------------------------


def test_desktop_profile_disables_all_classes(clean_capabilities):
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        for var in (rate_limit.ENV_MUTATING, rate_limit.ENV_READ, rate_limit.ENV_AUTH_FAILED):
            os.environ.pop(var, None)
        rate_limit.reset_for_test()
        assert rate_limit._default_for_class(rate_limit.CLASS_MUTATING) == 0
        assert rate_limit._default_for_class(rate_limit.CLASS_READ) == 0
        assert rate_limit._default_for_class(rate_limit.CLASS_AUTH_FAILED) == 0


def test_cloud_profile_enables_classes_with_defaults(clean_capabilities):
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}, clear=False):
        for var in (rate_limit.ENV_MUTATING, rate_limit.ENV_READ, rate_limit.ENV_AUTH_FAILED):
            os.environ.pop(var, None)
        rate_limit.reset_for_test()
        assert rate_limit._default_for_class(rate_limit.CLASS_MUTATING) == 30
        assert rate_limit._default_for_class(rate_limit.CLASS_READ) == 600
        assert rate_limit._default_for_class(rate_limit.CLASS_AUTH_FAILED) == 10


def test_explicit_env_overrides_profile_default(clean_capabilities):
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            rate_limit.ENV_MUTATING: "5",
        },
    ):
        rate_limit.reset_for_test()
        assert rate_limit._default_for_class(rate_limit.CLASS_MUTATING) == 5


def test_explicit_zero_disables_class_under_cloud(clean_capabilities):
    """``CPLUG_RATE_LIMIT_READ=0`` disables read-class rate limiting
    even in cloud profile."""
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            rate_limit.ENV_READ: "0",
        },
    ):
        rate_limit.reset_for_test()
        assert rate_limit._default_for_class(rate_limit.CLASS_READ) == 0


# ---------------------------------------------------------------------------
# Class registry — bucket lifecycle
# ---------------------------------------------------------------------------


def test_class_registry_disabled_passes_through(clean_capabilities):
    """capacity=0 -> always allowed, never charges a bucket."""
    reg = rate_limit._ClassRegistry(rate_limit.CLASS_READ)
    reg._capacity = 0
    allowed, limit, remaining, reset, retry = reg.take("k1", 0.0)
    assert allowed is True
    assert limit == 0


def test_class_registry_enforces_capacity(clean_capabilities):
    reg = rate_limit._ClassRegistry(rate_limit.CLASS_READ)
    reg._capacity = 2
    a, *_ = reg.take("k1", 0.0)
    b, *_ = reg.take("k1", 0.0)
    c, *_ = reg.take("k1", 0.0)
    assert a is True
    assert b is True
    assert c is False


def test_class_registry_distinct_keys_have_distinct_buckets(clean_capabilities):
    reg = rate_limit._ClassRegistry(rate_limit.CLASS_READ)
    reg._capacity = 1
    a, *_ = reg.take("k1", 0.0)
    b, *_ = reg.take("k2", 0.0)
    assert a is True and b is True


# ---------------------------------------------------------------------------
# Client-key resolution
# ---------------------------------------------------------------------------


def _scope(headers=None, client=("127.0.0.1", 0)):
    return {
        "type": "http",
        "method": "GET",
        "path": "/cplugapi/v1/health",
        "headers": headers or [],
        "client": client,
    }


def test_desktop_key_hashes_authorization(clean_capabilities):
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        rate_limit.reset_for_test()
        k1 = rate_limit._client_key(_scope([(b"authorization", b"Basic abc")]))
        k2 = rate_limit._client_key(_scope([(b"authorization", b"Basic xyz")]))
        k_noauth = rate_limit._client_key(_scope())
        assert k1 != k2
        assert k1.startswith("auth:")
        assert k_noauth == "auth:<none>"


def test_cloud_key_uses_client_ip(clean_capabilities):
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}, clear=False):
        os.environ.pop(rate_limit.ENV_TRUSTED_PROXIES, None)
        rate_limit.reset_for_test()
        k = rate_limit._client_key(_scope(client=("203.0.113.5", 0)))
        assert k == "ip:203.0.113.5"


def test_cloud_key_walks_xff_when_peer_trusted(clean_capabilities):
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            rate_limit.ENV_TRUSTED_PROXIES: "10.0.0.0/8",
        },
    ):
        rate_limit.reset_for_test()
        # Peer is in trusted CIDR; XFF chain is ``client, lb, proxy``.
        # Walk right-to-left, skipping trusted hops -> client.
        scope = _scope(
            headers=[(b"x-forwarded-for", b"203.0.113.5, 10.0.0.10, 10.0.0.20")],
            client=("10.0.0.20", 0),
        )
        k = rate_limit._client_key(scope)
        assert k == "ip:203.0.113.5"


def test_cloud_key_ignores_xff_when_peer_untrusted(clean_capabilities):
    """If the immediate peer is NOT in CPLUG_TRUSTED_PROXIES, ignore XFF
    entirely — the caller could have forged it."""
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            rate_limit.ENV_TRUSTED_PROXIES: "10.0.0.0/8",
        },
    ):
        rate_limit.reset_for_test()
        scope = _scope(
            headers=[(b"x-forwarded-for", b"203.0.113.5")],
            client=("198.51.100.50", 0),
        )
        k = rate_limit._client_key(scope)
        # peer untrusted -> use peer directly, ignore forged XFF
        assert k == "ip:198.51.100.50"


# ---------------------------------------------------------------------------
# Auth-failed wrap
# ---------------------------------------------------------------------------


def test_observe_auth_failures_passes_through_when_disabled(clean_capabilities):
    """Desktop profile + no env -> auth-failed bucket disabled; wrap
    is a pass-through that just delegates to the inner dep."""
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        os.environ.pop(rate_limit.ENV_AUTH_FAILED, None)
        rate_limit.reset_for_test()

        def inner(creds):
            if creds.password == "right":
                return creds
            raise HTTPException(status_code=401, detail="bad")

        wrapped = rate_limit.observe_auth_failures(inner)
        # Spam 100 failures — desktop has no cap.
        for _ in range(100):
            with pytest.raises(HTTPException):
                wrapped(HTTPBasicCredentials(username="u", password="wrong"))


def test_observe_auth_failures_throttles_after_n_failures(clean_capabilities):
    """When auth-failed is capped, after N failures the wrap raises 429
    BEFORE delegating — credential brute force is denied cheaply."""
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            rate_limit.ENV_AUTH_FAILED: "3",
        },
    ):
        rate_limit.reset_for_test()

        def inner(creds):
            raise HTTPException(status_code=401, detail="always fails")

        wrapped = rate_limit.observe_auth_failures(inner)
        # First 3 attempts -> 401 (delegate runs and rejects).
        for _ in range(3):
            with pytest.raises(HTTPException) as excinfo:
                wrapped(HTTPBasicCredentials(username="u", password="x"))
            assert excinfo.value.status_code == 401
        # 4th attempt -> 429 (bucket exhausted).
        with pytest.raises(HTTPException) as excinfo:
            wrapped(HTTPBasicCredentials(username="u", password="x"))
        assert excinfo.value.status_code == 429
        assert excinfo.value.headers["Retry-After"] == "60"


def test_observe_auth_failures_does_not_throttle_legitimate_user(clean_capabilities):
    """Distinct credentials get distinct buckets — an attacker hammering
    user A doesn't lock out user B."""
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud", rate_limit.ENV_AUTH_FAILED: "2"},
    ):
        rate_limit.reset_for_test()

        def inner(creds):
            if creds.username == "victim":
                raise HTTPException(status_code=401, detail="bad")
            return creds

        wrapped = rate_limit.observe_auth_failures(inner)
        # Burn through victim's bucket.
        for _ in range(2):
            with pytest.raises(HTTPException):
                wrapped(HTTPBasicCredentials(username="victim", password="bad"))
        with pytest.raises(HTTPException) as excinfo:
            wrapped(HTTPBasicCredentials(username="victim", password="bad"))
        assert excinfo.value.status_code == 429
        # Different user, fresh bucket -> auth dep runs and accepts.
        result = wrapped(HTTPBasicCredentials(username="other", password="ok"))
        assert result.username == "other"


def test_observe_auth_failures_does_not_charge_on_success(clean_capabilities):
    """Regression: pre-WB-scrutiny implementation pre-charged on EVERY
    call (including success), so legitimate clients making more than N
    authenticated requests/min got 429. After the fix, only 401s charge
    the bucket — successful auths never consume tokens.

    With AUTH_FAILED=2, a legitimate user making 100 successful calls
    should not see any 429."""
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud", rate_limit.ENV_AUTH_FAILED: "2"},
    ):
        rate_limit.reset_for_test()

        calls = []

        def inner(creds):
            calls.append(creds.username)
            return creds

        wrapped = rate_limit.observe_auth_failures(inner)
        for _ in range(100):
            result = wrapped(HTTPBasicCredentials(username="legit", password="ok"))
            assert result.username == "legit"
        assert len(calls) == 100


def test_observe_auth_failures_password_variation_does_not_bypass(clean_capabilities):
    """Regression: pre-WB-scrutiny keying hashed username+password-prefix(4),
    so varying the password past char 4 produced fresh buckets and let
    brute-force loop bypass the rate limit. After the fix, keying is
    username-only — every password variant against the same username
    contends on the same bucket."""
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud", rate_limit.ENV_AUTH_FAILED: "3"},
    ):
        rate_limit.reset_for_test()

        def inner(creds):
            raise HTTPException(status_code=401, detail="always fail")

        wrapped = rate_limit.observe_auth_failures(inner)
        # Try 10 different passwords against the same username.
        # Tracking distinct password-prefix(4) bytes to defeat the
        # OLD broken keying scheme.
        passwords = [
            "aaaa-1", "aaab-2", "aaac-3", "aaad-4", "aaae-5",
            "aaaf-6", "aaag-7", "aaah-8", "aaai-9", "aaaj-10",
        ]
        outcomes = []
        for pw in passwords:
            try:
                wrapped(HTTPBasicCredentials(username="target", password=pw))
            except HTTPException as e:
                outcomes.append(e.status_code)
        # With cap=3, the first 3 should be 401 (auth-dep ran and
        # rejected); from #4 onward, 429s should appear because the
        # bucket is empty regardless of password variation.
        assert outcomes[:3] == [401, 401, 401]
        # At least one 429 in the remaining 7 — the bucket is empty
        # and refills slowly, so the next attempts get 429.
        assert 429 in outcomes[3:], f"expected 429 after bucket exhausted; got {outcomes}"


# ---------------------------------------------------------------------------
# Middleware integration via setup_cplugapi
# ---------------------------------------------------------------------------


def _make_client():
    app = FastAPI()
    setup_cplugapi(app)
    rate_limit.install(app)
    app.middleware_stack = app.build_middleware_stack()
    return TestClient(app)


def test_middleware_emits_ratelimit_headers_when_active(clean_capabilities):
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud", rate_limit.ENV_READ: "10"},
    ):
        rate_limit.reset_for_test()
        client = _make_client()
        r = client.get(f"{PREFIX}/identify")
        assert r.status_code == 200
        assert r.headers.get("X-RateLimit-Limit") == "10"
        assert r.headers.get("X-RateLimit-Remaining") in ("9", "10")


def test_middleware_429_after_burst(clean_capabilities):
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud", rate_limit.ENV_READ: "3"},
    ):
        rate_limit.reset_for_test()
        client = _make_client()
        for _ in range(3):
            assert client.get(f"{PREFIX}/identify").status_code == 200
        r = client.get(f"{PREFIX}/identify")
        assert r.status_code == 429
        assert r.headers["content-type"].startswith(PROBLEM_JSON)
        body = r.json()
        assert body["code"] == CODES.RATE_LIMITED
        assert "Retry-After" in r.headers
        assert int(r.headers["Retry-After"]) >= 1


def test_middleware_no_headers_when_class_disabled(clean_capabilities):
    """Desktop default: read class is off; no rate-limit headers."""
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        for var in (rate_limit.ENV_MUTATING, rate_limit.ENV_READ, rate_limit.ENV_AUTH_FAILED):
            os.environ.pop(var, None)
        rate_limit.reset_for_test()
        client = _make_client()
        r = client.get(f"{PREFIX}/identify")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" not in r.headers


def test_middleware_passes_through_outside_prefix(clean_capabilities):
    """Invariant 1: /sdapi/v1/* is not touched by rate limit."""
    with patch.dict(
        os.environ,
        {profile.ENV_PROFILE: "cloud", rate_limit.ENV_READ: "1"},
    ):
        rate_limit.reset_for_test()
        app = FastAPI()
        setup_cplugapi(app)
        rate_limit.install(app)
        app.middleware_stack = app.build_middleware_stack()

        @app.get("/sdapi/v1/_test/x")
        def x():
            return {"ok": True}

        client = TestClient(app)
        # Burn through the cplugapi cap.
        client.get(f"{PREFIX}/identify")
        client.get(f"{PREFIX}/identify")
        # /sdapi/v1/* still serves — its scope didn't enter the
        # cplugapi rate limiter.
        for _ in range(5):
            r = client.get("/sdapi/v1/_test/x")
            assert r.status_code == 200
            assert "X-RateLimit-Limit" not in r.headers


# ---------------------------------------------------------------------------
# Startup validation (cloud profile fail-fast)
# ---------------------------------------------------------------------------


def test_validate_startup_passes_in_desktop(clean_capabilities, monkeypatch):
    """validate_startup() short-circuits in pytest (PYTEST_CURRENT_TEST
    set). Tests of the validation logic itself unset that env var so
    the real validation path runs."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.dict(os.environ, {profile.ENV_PROFILE: "desktop"}, clear=False):
        os.environ.pop(rate_limit.ENV_TRUSTED_PROXIES, None)
        rate_limit.reset_for_test()
        rate_limit.validate_startup()  # no raise


def test_validate_startup_passes_in_cloud_when_all_disabled(clean_capabilities, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            rate_limit.ENV_MUTATING: "0",
            rate_limit.ENV_READ: "0",
            rate_limit.ENV_AUTH_FAILED: "0",
        },
    ):
        rate_limit.reset_for_test()
        rate_limit.validate_startup()  # no raise


def test_validate_startup_fails_in_cloud_with_no_trusted_proxies(clean_capabilities, monkeypatch):
    """Cloud + any class enabled + no trusted proxies -> RuntimeError."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}, clear=False):
        os.environ.pop(rate_limit.ENV_TRUSTED_PROXIES, None)
        for var in (rate_limit.ENV_MUTATING, rate_limit.ENV_READ, rate_limit.ENV_AUTH_FAILED):
            os.environ.pop(var, None)
        rate_limit.reset_for_test()
        with pytest.raises(RuntimeError) as excinfo:
            rate_limit.validate_startup()
        assert "CPLUG_TRUSTED_PROXIES" in str(excinfo.value)


def test_validate_startup_passes_in_cloud_with_trusted_proxies(clean_capabilities, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch.dict(
        os.environ,
        {
            profile.ENV_PROFILE: "cloud",
            rate_limit.ENV_TRUSTED_PROXIES: "10.0.0.0/8",
        },
    ):
        rate_limit.reset_for_test()
        rate_limit.validate_startup()  # no raise


def test_validate_startup_skips_in_pytest_environment(clean_capabilities):
    """While running under pytest, validate_startup is a no-op even when
    cloud profile + classes-enabled + no trusted proxies — the bypass
    keeps unrelated tests from caring about this validation."""
    with patch.dict(os.environ, {profile.ENV_PROFILE: "cloud"}, clear=False):
        os.environ.pop(rate_limit.ENV_TRUSTED_PROXIES, None)
        rate_limit.reset_for_test()
        rate_limit.validate_startup()  # no raise — PYTEST_CURRENT_TEST is set


# ---------------------------------------------------------------------------
# Trusted-proxy parsing
# ---------------------------------------------------------------------------


def test_trusted_proxy_parsing_handles_multiple_cidrs(clean_capabilities):
    with patch.dict(
        os.environ,
        {rate_limit.ENV_TRUSTED_PROXIES: "10.0.0.0/8, 192.168.0.0/16, 2001:db8::/32"},
    ):
        nets = rate_limit._trusted_proxy_networks()
        assert len(nets) == 3


def test_trusted_proxy_parsing_drops_invalid_entries(clean_capabilities):
    with patch.dict(
        os.environ,
        {rate_limit.ENV_TRUSTED_PROXIES: "10.0.0.0/8,not-a-cidr,192.168.0.0/16"},
    ):
        nets = rate_limit._trusted_proxy_networks()
        assert len(nets) == 2


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def test_capability_registered(clean_capabilities):
    from modules.cplugapi import capabilities

    rate_limit.register_capabilities()
    assert "security/rate-limit" in capabilities.enabled_capabilities()
