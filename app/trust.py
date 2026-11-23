"""Host-scoped auto-approval rules.

Trust is exact-host: trusting ``login.example.com`` does not trust
``evil.example.com``. Trusted hosts bypass approval for browser write actions
and vault autofill on that host only. Users can revoke trust at any time.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .audit import AuditLog
from .db import Database, now_ts
from .vault import normalize_domain


class TrustedHosts:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    def list(self) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT host, created_at FROM trusted_hosts ORDER BY host"
        )

    def is_trusted(self, host: str) -> bool:
        domain = normalize_domain(host)
        if not domain:
            return False
        return (
            self.db.one("SELECT host FROM trusted_hosts WHERE host = ?", (domain,))
            is not None
        )

    def trust(self, host: str) -> List[Dict[str, Any]]:
        domain = normalize_domain(host)
        if not domain:
            raise ValueError("A hostname is required.")
        self.db.execute(
            "INSERT INTO trusted_hosts (host, created_at) VALUES (?, ?) "
            "ON CONFLICT(host) DO NOTHING",
            (domain, now_ts()),
        )
        self.audit.log("user", "trusted_host_added", domain, "ok")
        return self.list()

    def revoke(self, host: str) -> List[Dict[str, Any]]:
        domain = normalize_domain(host)
        if not domain:
            raise ValueError("A hostname is required.")
        cursor = self.db.execute(
            "DELETE FROM trusted_hosts WHERE host = ?", (domain,)
        )
        if cursor.rowcount:
            self.audit.log("user", "trusted_host_revoked", domain, "ok")
        return self.list()
