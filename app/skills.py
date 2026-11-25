"""Private, per-owner skills: reusable procedures loaded on demand.

Skills are scoped exactly like memory (``lead`` or ``agent:<id>``) and are not
shared between owners. The system prompt only lists names and summaries; the
full instructions are fetched with the skill_use tool when needed, which keeps
every agent's context small.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .db import Database

MAX_SKILLS_PER_SCOPE = 50
MAX_NAME = 80
MAX_SUMMARY = 200
MAX_INSTRUCTIONS = 8000


class SkillStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _clean_name(self, name: Any) -> str:
        return " ".join(str(name or "").split())[:MAX_NAME]

    def save(
        self,
        scope: str,
        name: str,
        instructions: str,
        summary: str = "",
    ) -> Dict[str, Any]:
        scope = str(scope or "lead")
        clean_name = self._clean_name(name)
        text = str(instructions or "").strip()[:MAX_INSTRUCTIONS]
        if not clean_name:
            raise ValueError("skill name is required.")
        if not text:
            raise ValueError("skills need instructions.")
        clean_summary = " ".join(str(summary or "").split())[:MAX_SUMMARY]
        if not clean_summary:
            clean_summary = text.splitlines()[0][:120]
        now = time.time()
        row = self.db.one(
            "SELECT id FROM agent_skills WHERE scope = ? AND lower(name) = lower(?)",
            (scope, clean_name),
        )
        if row:
            skill_id = int(row["id"])
            self.db.execute(
                "UPDATE agent_skills SET name = ?, summary = ?, instructions = ?, "
                "updated_at = ? WHERE id = ?",
                (clean_name, clean_summary, text, now, skill_id),
            )
        else:
            count = self.db.scalar(
                "SELECT COUNT(*) FROM agent_skills WHERE scope = ?", (scope,)
            ) or 0
            if int(count) >= MAX_SKILLS_PER_SCOPE:
                raise ValueError(
                    "Skill limit reached for this owner (%d). Delete one first."
                    % MAX_SKILLS_PER_SCOPE
                )
            skill_id = self.db.insert(
                "INSERT INTO agent_skills (scope, name, summary, instructions, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (scope, clean_name, clean_summary, text, now, now),
            )
        return self.get_by_id(skill_id) or {}

    def list(self, scope: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT id, name, summary, updated_at FROM agent_skills "
            "WHERE scope = ? ORDER BY lower(name) LIMIT ?",
            (str(scope or "lead"), max(1, min(int(limit), 100))),
        )

    def get(self, scope: str, name: str) -> Optional[Dict[str, Any]]:
        return self.db.one(
            "SELECT * FROM agent_skills WHERE scope = ? AND lower(name) = lower(?)",
            (str(scope or "lead"), str(name or "").strip()),
        )

    def get_by_id(self, skill_id: int) -> Optional[Dict[str, Any]]:
        return self.db.one(
            "SELECT * FROM agent_skills WHERE id = ?", (int(skill_id),)
        )

    def update(self, skill_id: int, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        current = self.get_by_id(skill_id)
        if not current:
            return None
        name = self._clean_name(values.get("name", current["name"]))
        summary = " ".join(
            str(values.get("summary", current["summary"]) or "").split()
        )[:MAX_SUMMARY]
        instructions = str(
            values.get("instructions", current["instructions"]) or ""
        ).strip()[:MAX_INSTRUCTIONS]
        if not name:
            raise ValueError("skill name is required.")
        if not instructions:
            raise ValueError("skills need instructions.")
        if not summary:
            summary = instructions.splitlines()[0][:120]
        self.db.execute(
            "UPDATE agent_skills SET name = ?, summary = ?, instructions = ?, "
            "updated_at = ? WHERE id = ?",
            (name, summary, instructions, time.time(), int(skill_id)),
        )
        return self.get_by_id(skill_id)

    def delete(self, scope: str, name: str) -> bool:
        row = self.get(scope, name)
        if not row:
            return False
        self.db.execute("DELETE FROM agent_skills WHERE id = ?", (int(row["id"]),))
        return True

    def delete_by_id(self, skill_id: int) -> bool:
        row = self.get_by_id(skill_id)
        if not row:
            return False
        self.db.execute("DELETE FROM agent_skills WHERE id = ?", (int(skill_id),))
        return True

    def counts(self) -> Dict[str, int]:
        rows = self.db.query(
            "SELECT scope, COUNT(*) AS n FROM agent_skills GROUP BY scope"
        )
        return {str(row["scope"]): int(row["n"]) for row in rows}
