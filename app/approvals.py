"""Approval inbox for consequential agent actions (Muse's Sentinel-style gate)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .db import Database, now_ts


class ApprovalError(ValueError):
    pass


class ApprovalQueue:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    def create(
        self,
        chat_id: str,
        tool: str,
        args: Dict[str, Any],
        summary: str = "",
        host: str = "",
    ) -> Dict[str, Any]:
        encoded = json.dumps(args, ensure_ascii=False)
        host = (host or "").strip().lower()
        existing = self.db.one(
            "SELECT id FROM approvals WHERE status = 'pending' AND chat_id = ? "
            "AND tool = ? AND args_json = ? AND host = ? ORDER BY id DESC LIMIT 1",
            (chat_id, tool, encoded, host),
        )
        if existing:
            return self.get(int(existing["id"]))
        approval_id = self.db.insert(
            "INSERT INTO approvals (chat_id, tool, args_json, summary, host, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (chat_id, tool, encoded, summary[:500], host, now_ts()),
        )
        self.audit.log(
            "agent",
            "approval_requested",
            {"id": approval_id, "tool": tool, "summary": summary},
            "pending",
        )
        return self.get(approval_id)

    def get(self, approval_id: int) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not row:
            raise ApprovalError("Approval %s not found." % approval_id)
        row["args"] = json.loads(row.pop("args_json") or "{}")
        return row

    def pending(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        for row in rows:
            row["args"] = json.loads(row.pop("args_json") or "{}")
        return rows

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM approvals ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        for row in rows:
            row["args"] = json.loads(row.pop("args_json") or "{}")
        return rows

    def claim(self, approval_id: int) -> bool:
        """Atomically take ownership of a pending approval before executing it."""
        cursor = self.db.execute(
            "UPDATE approvals SET status = 'executing', resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now_ts(), approval_id),
        )
        return cursor.rowcount == 1

    def resolve(
        self, approval_id: int, status: str, result: Optional[str] = None
    ) -> Dict[str, Any]:
        if status not in ("approved", "rejected", "expired"):
            raise ApprovalError("Invalid approval status.")
        row = self.db.one(
            "SELECT status FROM approvals WHERE id = ?", (approval_id,)
        )
        if not row:
            raise ApprovalError("Approval %s not found." % approval_id)
        if row["status"] not in ("pending", "executing"):
            raise ApprovalError("Approval %s is already resolved." % approval_id)
        self.db.execute(
            "UPDATE approvals SET status = ?, result = ?, resolved_at = ? "
            "WHERE id = ? AND status IN ('pending', 'executing')",
            (status, (result or "")[:8000], now_ts(), approval_id),
        )
        self.audit.log("user", "approval_%s" % status, approval_id, status)
        return self.get(approval_id)

    def expire_stale(
        self, max_age_seconds: int = 86_400, executing_max_age: int = 3_600
    ) -> int:
        cutoff = time.time() - max_age_seconds
        stale = self.db.query(
            "SELECT id FROM approvals WHERE status = 'pending' AND created_at < ?",
            (cutoff,),
        )
        for row in stale:
            self.db.execute(
                "UPDATE approvals SET status = 'expired', resolved_at = ? WHERE id = ?",
                (now_ts(), row["id"]),
            )
            self.audit.log("system", "approval_expired", row["id"], "expired")
        stuck_cutoff = time.time() - executing_max_age
        stuck = self.db.query(
            "SELECT id FROM approvals WHERE status = 'executing' AND resolved_at < ?",
            (stuck_cutoff,),
        )
        for row in stuck:
            self.db.execute(
                "UPDATE approvals SET status = 'expired', result = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'executing'",
                ("Interrupted while executing.", now_ts(), row["id"]),
            )
            self.audit.log("system", "approval_expired", row["id"], "expired")
        return len(stale) + len(stuck)
