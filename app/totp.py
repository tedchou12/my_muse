"""Small, dependency-free TOTP helper for vault-backed 2FA."""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse


class TotpError(ValueError):
    pass


def normalize_secret(value: str) -> str:
    raw = (value or "").strip()
    if raw.lower().startswith("otpauth://"):
        parsed = urlparse(raw)
        raw = (parse_qs(parsed.query).get("secret") or [""])[0]
    raw = raw.replace(" ", "").replace("-", "").upper().rstrip("=")
    if not raw:
        raise TotpError("TOTP secret is empty.")
    padding = "=" * ((8 - len(raw) % 8) % 8)
    try:
        base64.b32decode(raw + padding, casefold=True)
    except Exception as exc:
        raise TotpError("TOTP secret is not valid base32.") from exc
    return raw + padding


def code(
    secret: str,
    timestamp: Optional[float] = None,
    digits: int = 6,
    period: int = 30,
) -> str:
    key = base64.b32decode(normalize_secret(secret), casefold=True)
    counter = int((timestamp if timestamp is not None else time.time()) // period)
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10**digits)).zfill(digits)
