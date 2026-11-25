"""Private, per-owner memory facts (lead or one named agent)."""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .db import Database

MAX_FACT_CHARS = 1000


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def remember(self, scope: str, content: str, source: str = "") -> int:
        text = str(content or "").strip()[:MAX_FACT_CHARS]
        if not text:
            raise ValueError("fact is required.")
        return self.db.insert(
            "INSERT INTO memories (content, source, scope, created_at) "
            "VALUES (?, ?, ?, ?)",
            (text, str(source or "")[:100], str(scope or "lead"), time.time()),
        )

    def recall(
        self, scope: str, query: str = "", limit: int = 10
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        search = str(query or "").strip()[:200]
        if search:
            return self.db.query(
                "SELECT id, content, created_at FROM memories "
                "WHERE scope = ? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (scope, "%" + search + "%", limit),
            )
        return self.db.query(
            "SELECT id, content, created_at FROM memories WHERE scope = ? "
            "ORDER BY id DESC LIMIT ?",
            (scope, limit),
        )

    def recent(self, scope: str, limit: int = 12) -> List[Dict[str, Any]]:
        return self.recall(scope, "", limit)

    def list_scope(self, scope: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT id, content, source, created_at FROM memories "
            "WHERE scope = ? ORDER BY id DESC LIMIT ?",
            (scope, max(1, min(int(limit), 500))),
        )

    def delete(self, memory_id: int) -> bool:
        row = self.db.one("SELECT id FROM memories WHERE id = ?", (int(memory_id),))
        if not row:
            return False
        self.db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
        return True

    def counts(self) -> Dict[str, int]:
        rows = self.db.query(
            "SELECT scope, COUNT(*) AS n FROM memories GROUP BY scope"
        )
        return {str(row["scope"]): int(row["n"]) for row in rows}
