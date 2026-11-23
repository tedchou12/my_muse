"""Session cookies, CSRF guard and login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

COOKIE_NAME = "muse_session"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()


def create_session(secret: str, ttl_seconds: int) -> str:
    body = json.dumps(
        {"exp": int(time.time()) + int(ttl_seconds), "sid": secrets.token_hex(8)},
        separators=(",", ":"),
    )
    payload = _b64encode(body.encode("utf-8"))
    return "%s.%s" % (payload, _sign(payload, secret))


def verify_session(token: Optional[str], secret: str) -> bool:
    if not token or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return False
    try:
        data = json.loads(_b64decode(payload).decode("utf-8"))
    except (ValueError, TypeError):
        return False
    return int(data.get("exp", 0)) > int(time.time())


class SlidingWindowLimiter:
    """Tiny in-memory throttle for login and vault unlock attempts."""

    def __init__(self, max_events: int = 5, window_seconds: int = 60) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._events: Dict[str, Deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._events.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.max_events:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> int:
        bucket = self._events.get(key)
        if not bucket:
            return 0
        remaining = self.window - (time.monotonic() - bucket[0])
        return max(1, int(remaining) + 1)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)


def client_key(host: Optional[str]) -> str:
    return (host or "unknown").strip()[:80]


def password_matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(supplied.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def request_origin_ok(origin: Optional[str], host: Optional[str]) -> bool:
    """Defence in depth on top of SameSite=Lax cookies."""
    if not origin:
        return True
    try:
        from urllib.parse import urlparse

        parsed = urlparse(origin)
        origin_host = parsed.netloc
    except ValueError:
        return False
    return bool(origin_host) and bool(host) and origin_host == host


def parse_basic_auth(header: Optional[str]) -> Tuple[str, str]:
    if not header or not header.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
        user, _, password = decoded.partition(":")
        return user, password
    except (ValueError, UnicodeDecodeError):
        return "", ""
