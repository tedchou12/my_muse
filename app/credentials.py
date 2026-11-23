"""Credential requests: the bridge between "Muse needs a login" and the vault.

The agent never asks for a password in chat. It calls ``request_credential`` on a
login page; the UI shows a card; the user's submission is encrypted into the
vault and used to submit the login form in one explicit action.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .db import Database, now_ts
from .vault import normalize_domain


class CredentialRequestError(ValueError):
    pass


class CredentialRequestQueue:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    def create(
        self,
        chat_id: str,
        label: str,
        domain: str,
        login_url: str = "",
        reason: str = "",
        username_selector: str = "",
        password_selector: str = "",
        submit_selector: str = "",
        kind: str = "login",
        otp_selector: str = "",
    ) -> Dict[str, Any]:
        domain = normalize_domain(domain)
        if not domain:
            raise CredentialRequestError("A domain is required.")
        kind = (kind or "login").strip().lower()
        if kind not in ("login", "vault", "otp"):
            raise CredentialRequestError("Invalid credential request kind.")
        label = (label or domain).strip()[:80] or domain
        existing = self.db.one(
            "SELECT id FROM credential_requests WHERE status = 'pending' "
            "AND chat_id = ? AND domain = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, domain, kind),
        )
        if existing:
            return self.get(int(existing["id"]))
        request_id = self.db.insert(
            "INSERT INTO credential_requests (chat_id, label, domain, login_url, "
            "reason, username_selector, password_selector, submit_selector, kind, "
            "otp_selector, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                chat_id,
                label,
                domain,
                login_url[:2048],
                reason[:1000],
                username_selector[:500],
                password_selector[:500],
                submit_selector[:500],
                kind,
                otp_selector[:500],
                now_ts(),
            ),
        )
        self.audit.log(
            "agent",
            "credential_requested",
            {"id": request_id, "domain": domain, "label": label, "kind": kind},
            "pending",
        )
        return self.get(request_id)

    def get(self, request_id: int) -> Dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM credential_requests WHERE id = ?", (request_id,)
        )
        if not row:
            raise CredentialRequestError("Credential request not found.")
        return row

    def pending(
        self, chat_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if chat_id:
            return self.db.query(
                "SELECT * FROM credential_requests WHERE status = 'pending' "
                "AND chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            )
        return self.db.query(
            "SELECT * FROM credential_requests WHERE status = 'pending' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def recent(self, limit: int = 25) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM credential_requests ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )

    def resolve(
        self,
        request_id: int,
        status: str,
        entry_id: Optional[int] = None,
        result: str = "",
    ) -> Dict[str, Any]:
        if status not in ("fulfilled", "cancelled", "expired"):
            raise CredentialRequestError("Invalid credential request status.")
        row = self.get(request_id)
        if row["status"] != "pending":
            raise CredentialRequestError("This credential request is already resolved.")
        self.db.execute(
            "UPDATE credential_requests SET status = ?, entry_id = ?, result = ?, "
            "resolved_at = ? WHERE id = ? AND status = 'pending'",
            (status, entry_id, (result or "")[:4000], now_ts(), request_id),
        )
        self.audit.log(
            "user",
            "credential_%s" % status,
            {"id": request_id, "domain": row["domain"], "label": row["label"], "entry_id": entry_id},
            status,
        )
        return self.get(request_id)

    def expire_stale(self, max_age_seconds: int = 86_400) -> int:
        cutoff = time.time() - max_age_seconds
        rows = self.db.query(
            "SELECT id FROM credential_requests WHERE status = 'pending' "
            "AND created_at < ?",
            (cutoff,),
        )
        for row in rows:
            self.db.execute(
                "UPDATE credential_requests SET status = 'expired', resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (now_ts(), row["id"]),
            )
            self.audit.log("system", "credential_expired", row["id"], "expired")
        return len(rows)
