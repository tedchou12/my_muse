from __future__ import annotations

import time

from app.security import (
    SlidingWindowLimiter,
    create_session,
    password_matches,
    request_origin_ok,
    verify_session,
)


def test_session_roundtrip():
    secret = "s" * 32
    token = create_session(secret, 60)
    assert verify_session(token, secret) is True
    assert verify_session(token, "different") is False
    payload, signature = token.rsplit(".", 1)
    replacement = "0" if signature[-1] != "0" else "1"
    assert (
        verify_session(payload + "." + signature[:-1] + replacement, secret) is False
    )
    assert verify_session(None, secret) is False


def test_session_expiry(monkeypatch):
    secret = "s" * 32
    token = create_session(secret, 60)
    real_time = time.time
    monkeypatch.setattr("app.security.time.time", lambda: real_time() + 3600)
    assert verify_session(token, secret) is False


def test_password_compare():
    assert password_matches("abc", "abc") is True
    assert password_matches("abc", "abd") is False


def test_sliding_window_limiter():
    limiter = SlidingWindowLimiter(max_events=2, window_seconds=60)
    assert limiter.allow("ip") is True
    assert limiter.allow("ip") is True
    assert limiter.allow("ip") is False
    assert limiter.retry_after("ip") >= 1
    limiter.reset("ip")
    assert limiter.allow("ip") is True


def test_origin_check():
    assert request_origin_ok(None, "localhost:8000") is True
    assert request_origin_ok("http://localhost:8000", "localhost:8000") is True
    assert request_origin_ok("https://evil.example", "localhost:8000") is False
