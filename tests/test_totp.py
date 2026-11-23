from __future__ import annotations

import pytest

from app.totp import TotpError, code, normalize_secret


def test_rfc6238_sha1_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert code(secret, timestamp=59, digits=8) == "94287082"


def test_otpauth_uri_and_invalid_secret():
    assert (
        normalize_secret(
            "otpauth://totp/GitHub:me?secret=JBSWY3DPEHPK3PXP&issuer=GitHub"
        )
        == "JBSWY3DPEHPK3PXP"
    )
    with pytest.raises(TotpError):
        code("not base32!")
