"""Append-only audit trail: every consequential action is recorded."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .db import Database, now_ts


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def log(
        self,
        actor: str,
        action: str,
        detail: Any = "",
        outcome: str = "ok",
    ) -> None:
        if not isinstance(detail, str):
            try:
                detail = json.dumps(detail, ensure_ascii=False)[:4000]
            except (TypeError, ValueError):
                detail = str(detail)[:4000]
        self.db.execute(
            "INSERT INTO audit (ts, actor, action, detail, outcome) VALUES (?, ?, ?, ?, ?)",
            (now_ts(), actor[:80], action[:120], detail[:4000], outcome[:40]),
        )

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        return self.db.query(
            "SELECT id, ts, actor, action, detail, outcome FROM audit "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )


class Preferences:
    """Small JSON key/value store for UI-editable settings."""

    DEFAULTS = {
        "agent_name": "Muse",
        "agent_style": "warm, concise, proactive, and honest about limits",
        "write_enabled": True,
        "auto_approve_writes": False,
        "auto_approve_browser": True,
        "shell_enabled": False,
        "trusted_shell_commands": [],
        "trusted_shell_families": [],
        "trusted_shell_families_initialized": False,
        "timezone": "UTC",
    }

    def __init__(self, db: Database, defaults: Optional[Dict[str, Any]] = None) -> None:
        self.db = db
        self._defaults = dict(self.DEFAULTS)
        if defaults:
            self._defaults.update(defaults)

    def get(self, key: str) -> Any:
        row = self.db.one("SELECT value FROM settings WHERE key = ?", (key,))
        if not row:
            return self._defaults.get(key)
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def all(self) -> Dict[str, Any]:
        data = dict(self._defaults)
        for row in self.db.query("SELECT key, value FROM settings"):
            if row["key"] in self._defaults:
                try:
                    data[row["key"]] = json.loads(row["value"])
                except (TypeError, ValueError):
                    data[row["key"]] = row["value"]
        return data

    def update(self, values: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in values.items():
            if key not in self._defaults:
                continue
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        return self.all()
