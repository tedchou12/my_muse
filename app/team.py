"""Named AI teammates, delegation, inter-agent messages and their runner.

This is the Grok Bot-inspired layer: every agent is a narrow role with its own
instructions, team tasks are durable rows, and agents can hand work to each
other with ``assign_task`` / ``send_message``. External side effects still go
through the existing tool approval gate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditLog
from .config import Settings
from .db import Database

logger = logging.getLogger("muse.team")

AGENT_STATUSES = {"active", "paused", "archived"}
OPEN_TASK_STATUSES = ("queued", "working", "needs_approval")
TASK_STATUSES = {"queued", "working", "needs_approval", "done", "failed", "cancelled"}
MUSE_ID = 0


class TeamError(RuntimeError):
    pass


class AgentNotFound(TeamError):
    pass


class TaskNotFound(TeamError):
    pass


class DuplicateAgent(TeamError):
    pass


class TeamLimit(TeamError):
    pass


def _clean(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _name_from_brief(brief: str, fallback: str = "Specialist") -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", brief)
    if not words:
        return fallback
    stop = {"a", "an", "the", "who", "that", "for", "and", "with", "to", "of"}
    picked = [w for w in words if w.lower() not in stop][:4]
    name = " ".join(word.capitalize() for word in picked) or fallback
    return name[:60]


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse_chat_context(chat_id: str) -> Tuple[Optional[int], Optional[int]]:
    """Return (agent_id, task_id) encoded in runner chat ids.

    Runner chats look like ``agent:4:task:12``. Normal chats (``default``)
    return (None, None) and are treated as the human/manager.
    """
    agent_match = re.search(r"(?:^|:)agent:(\d+)", chat_id or "")
    task_match = re.search(r"(?:^|:)task:(\d+)", chat_id or "")
    return (
        int(agent_match.group(1)) if agent_match else None,
        int(task_match.group(1)) if task_match else None,
    )

def _clean_commands(values: Any) -> List[str]:
    """Normalise a trusted-command list: unique, trimmed, capped."""
    if isinstance(values, str):
        text = values.strip()
        if text.startswith("["):
            try:
                values = json.loads(text)
            except (TypeError, ValueError):
                values = [text]
        else:
            values = [text]
    if not isinstance(values, (list, tuple)):
        return []
    items: List[str] = []
    for raw in values[:50]:
        text = _clean(raw, 200)
        if text and text not in items:
            items.append(text)
    return items


class TeamStore:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        settings: Settings,
        openai: Any = None,
        prefs: Any = None,
    ) -> None:
        self.db = db
        self.audit = audit
        self.settings = settings
        self.openai = openai
        self.prefs = prefs

    # -- lead identity ----------------------------------------------------
    def lead_name(self) -> str:
        """The current display name of the team lead (renamed in Settings)."""
        name = ""
        if self.prefs is not None:
            try:
                name = str(self.prefs.get("agent_name") or "").strip()
            except Exception:
                name = ""
        return name or "Muse"

    def lead_profile(self) -> Dict[str, Any]:
        return {
            "id": MUSE_ID,
            "name": self.lead_name(),
            "role": "Team lead",
            "status": "active",
            "model": "",
            "instructions": "",
        }

    # -- agents -----------------------------------------------------------
    def _agent_select(self) -> str:
        return (
            "SELECT a.*, "
            "(SELECT COUNT(*) FROM agent_tasks t WHERE t.agent_id = a.id "
            " AND t.status IN ('queued','working','needs_approval')) AS open_tasks, "
            "(SELECT COUNT(*) FROM agent_tasks t WHERE t.agent_id = a.id "
            " AND t.status = 'done') AS done_tasks "
            "FROM agents a "
        )

    def get_agent(
        self, agent_id: Optional[int] = None, name: Optional[str] = None
    ) -> Dict[str, Any]:
        row = None
        if agent_id:
            row = self.db.one(
                self._agent_select() + "WHERE a.id = ?", (int(agent_id),)
            )
        elif name:
            row = self.db.one(
                self._agent_select() + "WHERE lower(a.name) = lower(?)",
                (name.strip(),),
            )
        if not row:
            raise AgentNotFound("Agent not found.")
        return row

    def list_agents(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        where = "" if include_archived else "WHERE a.status != 'archived' "
        return self.db.query(self._agent_select() + where + "ORDER BY a.id")

    def create_agent(
        self,
        name: str,
        role: str,
        instructions: str,
        created_by: str = "user",
        model: str = "",
        trusted_commands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        name = _clean(name, 60)
        role = _clean(role, 200)
        instructions = _clean(instructions, 8000) or role
        model = _clean(model, 160)
        trust = _clean_commands(trusted_commands)
        if not name or not role:
            raise TeamError("Agent name and role are required.")
        active = self.db.scalar(
            "SELECT COUNT(*) FROM agents WHERE status != 'archived'"
        ) or 0
        if int(active) >= max(1, int(self.settings.team_max_agents)):
            raise TeamLimit(
                "Team limit reached (%d active agents). Archive one first."
                % self.settings.team_max_agents
            )
        if self.db.one(
            "SELECT id FROM agents WHERE lower(name) = lower(?)", (name,)
        ):
            raise DuplicateAgent("An agent named %r already exists." % name)
        now = time.time()
        agent_id = self.db.insert(
            "INSERT INTO agents (name, role, instructions, model, trusted_commands, "
            "status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                name,
                role,
                instructions,
                model,
                json.dumps(trust),
                _clean(created_by, 80),
                now,
                now,
            ),
        )
        self.audit.log(
            _clean(created_by, 80) or "user",
            "agent_created",
            {
                "agent_id": agent_id,
                "name": name,
                "role": role,
                "model": model,
                "trusted_commands": trust,
            },
        )
        return self.get_agent(agent_id)

    def update_agent(
        self, agent_id: int, values: Dict[str, Any], actor: str = "user"
    ) -> Dict[str, Any]:
        current = self.get_agent(agent_id)
        fields: Dict[str, Any] = {}
        for key in (
            "name",
            "role",
            "instructions",
            "model",
            "status",
            "trusted_commands",
        ):
            if key in values and values[key] is not None:
                if key == "trusted_commands":
                    fields[key] = json.dumps(_clean_commands(values[key]))
                    continue
                limit = {
                    "name": 60,
                    "role": 200,
                    "instructions": 8000,
                    "model": 160,
                    "status": 20,
                }[key]
                fields[key] = _clean(values[key], limit)
        if "status" in fields and fields["status"] not in AGENT_STATUSES:
            raise TeamError("Unknown agent status.")
        if not fields:
            return current
        if "name" in fields and not fields["name"]:
            raise TeamError("Agent name cannot be empty.")
        if "name" in fields:
            duplicate = self.db.one(
                "SELECT id FROM agents WHERE lower(name) = lower(?) AND id != ?",
                (fields["name"], agent_id),
            )
            if duplicate:
                raise DuplicateAgent("An agent named %r already exists." % fields["name"])
        fields["updated_at"] = time.time()
        assignments = ", ".join("%s = ?" % key for key in fields)
        self.db.execute(
            "UPDATE agents SET %s WHERE id = ?" % assignments,
            tuple(fields.values()) + (int(agent_id),),
        )
        self.audit.log(actor, "agent_updated", {"agent_id": agent_id, "fields": list(fields)})
        return self.get_agent(agent_id)

    def archive_agent(self, agent_id: int) -> Dict[str, Any]:
        self.get_agent(agent_id)
        now = time.time()
        self.db.execute(
            "UPDATE agents SET status = 'archived', updated_at = ? WHERE id = ?",
            (now, int(agent_id)),
        )
        self.db.execute(
            "UPDATE agent_tasks SET status = 'cancelled', finished_at = ?, "
            "error = 'Agent archived' WHERE agent_id = ? "
            "AND status IN ('queued','needs_approval')",
            (now, int(agent_id)),
        )
        self.audit.log("user", "agent_archived", {"agent_id": agent_id})
        return self.get_agent(agent_id)


    def _unique_name(self, candidate: str) -> str:
        base = _clean(candidate, 60) or "Specialist"
        name = base
        index = 2
        while self.db.one(
            "SELECT id FROM agents WHERE lower(name) = lower(?)", (name,)
        ):
            suffix = " %d" % index
            name = base[: 60 - len(suffix)] + suffix
            index += 1
        return name

    async def design_agent(self, brief: str) -> Dict[str, str]:
        """Design a narrow teammate from a free-text brief.

        Uses the configured model when available; falls back to a deterministic
        split of the brief so agent creation still works offline.
        """
        brief = _clean(brief, 8000)
        if len(brief) < 5:
            raise TeamError("Describe the agent's job in at least a few words.")
        fallback = {
            "name": _name_from_brief(brief),
            "role": brief.splitlines()[0][:200],
            "instructions": brief,
        }
        if not getattr(self.openai, "configured", False):
            return fallback
        system = (
            "You design one narrow teammate for a multi-agent team. "
            "Return ONLY a JSON object with keys name, role, instructions. "
            "name is a short 2-3 word title; role is one sentence; instructions "
            "is 3-6 sentences covering responsibilities, inputs, outputs, "
            "boundaries, and when to delegate. No markdown fences."
        )
        try:
            text = await self.openai.agenerate_text(
                "Brief:\n" + brief,
                system=system,
                max_words=400,
                session_id="team:design",
            )
            data = _parse_json_object(text)
            if data:
                name = _clean(data.get("name"), 60)
                role = _clean(data.get("role"), 200)
                instructions = _clean(data.get("instructions"), 8000)
                if name and role and instructions:
                    return {
                        "name": self._unique_name(name),
                        "role": role,
                        "instructions": instructions,
                    }
        except Exception:
            logger.exception("Agent design model call failed")
        fallback["name"] = self._unique_name(fallback["name"])
        return fallback

    async def create_from_prompt(
        self,
        brief: str,
        name: Optional[str] = None,
        role: Optional[str] = None,
        created_by: str = "user",
        model: Optional[str] = None,
        trusted_commands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        profile = await self.design_agent(brief)
        chosen_name = _clean(name, 60) or profile["name"]
        chosen_role = _clean(role, 200) or profile["role"]
        return self.create_agent(
            chosen_name,
            chosen_role,
            profile["instructions"],
            created_by=created_by,
            model=model or "",
            trusted_commands=trusted_commands,
        )


    # -- tasks ------------------------------------------------------------
    def _task_select(self) -> str:
        return (
            "SELECT t.*, a.name AS agent_name, c.name AS created_by_name "
            "FROM agent_tasks t "
            "LEFT JOIN agents a ON a.id = t.agent_id "
            "LEFT JOIN agents c ON c.id = t.created_by_agent_id "
        )

    def get_task(self, task_id: int) -> Dict[str, Any]:
        row = self.db.one(
            self._task_select() + "WHERE t.id = ?", (int(task_id),)
        )
        if not row:
            raise TaskNotFound("Team task not found.")
        return row

    def list_tasks(
        self,
        limit: int = 50,
        agent_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if agent_id:
            clauses.append("t.agent_id = ?")
            params.append(int(agent_id))
        if status:
            clauses.append("t.status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        return self.db.query(
            self._task_select() + where + "ORDER BY t.id DESC LIMIT ?", tuple(params)
        )

    def create_task(
        self,
        title: str,
        prompt: str,
        agent_id: int,
        created_by_agent_id: Optional[int] = None,
        parent_task_id: Optional[int] = None,
        actor: str = "user",
    ) -> Dict[str, Any]:
        title = _clean(title, 120)
        prompt = _clean(prompt, 8000)
        if not title or not prompt:
            raise TeamError("Task title and prompt are required.")
        agent = self.get_agent(agent_id)
        if agent["status"] != "active":
            raise TeamError("That agent is not active.")
        if created_by_agent_id:
            self.get_agent(created_by_agent_id)
        parent = None
        depth = 0
        if parent_task_id:
            parent = self.get_task(parent_task_id)
            depth = int(parent["depth"]) + 1
            if depth > max(0, int(self.settings.team_max_depth)):
                raise TeamLimit(
                    "Delegation depth limit reached (%d). Finish this task "
                    "instead of delegating again." % self.settings.team_max_depth
                )
        open_count = self.db.scalar(
            "SELECT COUNT(*) FROM agent_tasks WHERE status IN "
            "('queued','working','needs_approval')"
        ) or 0
        if int(open_count) >= 100:
            raise TeamLimit("Too many open team tasks. Finish or cancel some first.")
        now = time.time()
        task_id = self.db.insert(
            "INSERT INTO agent_tasks (title, prompt, agent_id, "
            "created_by_agent_id, parent_task_id, status, depth, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                title,
                prompt,
                int(agent_id),
                int(created_by_agent_id) if created_by_agent_id else None,
                int(parent_task_id) if parent_task_id else None,
                depth,
                now,
            ),
        )
        sender = "user"
        if created_by_agent_id:
            sender = "agent:%d" % int(created_by_agent_id)
        self.audit.log(
            sender,
            "team_task_created",
            {
                "task_id": task_id,
                "agent_id": int(agent_id),
                "parent_task_id": parent_task_id,
                "depth": depth,
            },
        )
        message = "Task #%d assigned: %s\n\n%s" % (task_id, title, prompt)
        self.send_message(
            from_agent_id=created_by_agent_id,
            to_agent_id=int(agent_id),
            content=message,
            task_id=task_id,
            kind="assignment",
            actor=sender,
        )
        return self.get_task(task_id)


    def _claim(self, task_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self.db.transaction() as conn:
            if task_id is None:
                row = conn.execute(
                    "SELECT t.id FROM agent_tasks t "
                    "JOIN agents a ON a.id = t.agent_id "
                    "WHERE t.status = 'queued' AND a.status = 'active' "
                    "ORDER BY t.id LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM agent_tasks WHERE id = ? "
                    "AND status IN ('queued','needs_approval')",
                    (int(task_id),),
                ).fetchone()
            if not row:
                return None
            claimed = conn.execute(
                "UPDATE agent_tasks SET status = 'working', started_at = ?, "
                "finished_at = NULL, error = NULL, run_count = run_count + 1 "
                "WHERE id = ? AND status IN ('queued','needs_approval')",
                (now, int(row["id"])),
            )
            if claimed.rowcount != 1:
                return None
            claimed_id = int(row["id"])
        return self.get_task(claimed_id)

    def claim_next(self) -> Optional[Dict[str, Any]]:
        return self._claim()

    def claim_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        return self._claim(int(task_id))

    def _notify_creator(
        self,
        task: Dict[str, Any],
        content: str,
        kind: str = "result",
    ) -> None:
        creator = task.get("created_by_agent_id")
        try:
            self.send_message(
                from_agent_id=task.get("agent_id"),
                to_agent_id=int(creator) if creator else None,
                content=content,
                task_id=int(task["id"]),
                kind=kind,
                actor="agent:%s" % task.get("agent_id"),
            )
        except Exception:
            logger.exception("Could not notify creator agent %s", creator)

    def complete_task(self, task_id: int, result: str) -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "working":
            return task
        result = _clean(result, 12000) or "Done."
        now = time.time()
        self.db.execute(
            "UPDATE agent_tasks SET status = 'done', result = ?, error = NULL, "
            "finished_at = ? WHERE id = ? AND status = 'working'",
            (result, now, int(task_id)),
        )
        task = self.get_task(task_id)
        self.audit.log(
            "agent:%s" % task.get("agent_id"),
            "team_task_completed",
            {"task_id": int(task_id), "agent_id": task.get("agent_id")},
        )
        self._notify_creator(
            task, "Task #%d complete: %s\n\n%s" % (task_id, task["title"], result)
        )
        return task

    def block_task(self, task_id: int, reply: str, reason: str = "") -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "working":
            return task
        reply = _clean(reply, 12000) or "Waiting for approval."
        self.db.execute(
            "UPDATE agent_tasks SET status = 'needs_approval', result = ?, "
            "error = ?, finished_at = ? WHERE id = ? AND status = 'working'",
            (reply, _clean(reason, 1000), time.time(), int(task_id)),
        )
        task = self.get_task(task_id)
        self._notify_creator(
            task,
            "Task #%d is blocked and needs your input: %s" % (task_id, reply),
            kind="blocked",
        )
        return task

    def fail_task(self, task_id: int, error: str) -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] == "cancelled":
            return task
        error = _clean(error, 4000) or "Task failed."
        self.db.execute(
            "UPDATE agent_tasks SET status = 'failed', error = ?, "
            "result = CASE WHEN result IS NULL OR result = '' THEN ? ELSE result END, "
            "finished_at = ? WHERE id = ? "
            "AND status IN ('working','queued','needs_approval')",
            (error, error, time.time(), int(task_id)),
        )
        task = self.get_task(task_id)
        self.audit.log(
            "agent:%s" % task.get("agent_id"),
            "team_task_failed",
            {"task_id": int(task_id), "error": error[:500]},
            "error",
        )
        self._notify_creator(
            task, "Task #%d failed: %s" % (task_id, error), kind="error"
        )
        return task

    def cancel_task(self, task_id: int) -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] in ("done", "failed", "cancelled"):
            return task
        self.db.execute(
            "UPDATE agent_tasks SET status = 'cancelled', finished_at = ?, "
            "error = 'Cancelled by user' WHERE id = ?",
            (time.time(), int(task_id)),
        )
        self.audit.log("user", "team_task_cancelled", {"task_id": int(task_id)})
        return self.get_task(task_id)

    def requeue_task(self, task_id: int) -> None:
        self.db.execute(
            "UPDATE agent_tasks SET status = 'queued', started_at = NULL, "
            "finished_at = NULL WHERE id = ? AND status = 'working'",
            (int(task_id),),
        )

    def recover_working(self) -> int:
        cur = self.db.execute(
            "UPDATE agent_tasks SET status = 'queued', started_at = NULL "
            "WHERE status = 'working'"
        )
        return int(cur.rowcount or 0)


    # -- messages ---------------------------------------------------------
    def send_message(
        self,
        from_agent_id: Optional[int],
        to_agent_id: Optional[int],
        content: str,
        task_id: Optional[int] = None,
        kind: str = "message",
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        content = _clean(content, 8000)
        if not content:
            raise TeamError("Message cannot be empty.")
        if to_agent_id:
            self.get_agent(to_agent_id)
        if from_agent_id:
            self.get_agent(from_agent_id)
        message_id = self.db.insert(
            "INSERT INTO agent_messages (from_agent_id, to_agent_id, task_id, "
            "kind, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(from_agent_id) if from_agent_id else None,
                int(to_agent_id) if to_agent_id else None,
                int(task_id) if task_id else None,
                _clean(kind, 40) or "message",
                content,
                time.time(),
            ),
        )
        self.audit.log(
            actor or ("agent:%s" % from_agent_id if from_agent_id else "user"),
            "agent_message",
            {
                "message_id": message_id,
                "to_agent_id": int(to_agent_id) if to_agent_id else None,
                "kind": kind,
            },
        )
        return self.db.one(
            "SELECT m.*, COALESCE(f.name, 'User') AS from_name, "
            "t.name AS to_name FROM agent_messages m "
            "LEFT JOIN agents f ON f.id = m.from_agent_id "
            "LEFT JOIN agents t ON t.id = m.to_agent_id WHERE m.id = ?",
            (message_id,),
        )

    def inbox(
        self,
        agent_id: Optional[int],
        limit: int = 20,
        unread_only: bool = False,
        mark_read: bool = False,
    ) -> List[Dict[str, Any]]:
        if agent_id:
            self.get_agent(agent_id)
            where = "m.to_agent_id = ?"
            key: Any = int(agent_id)
        else:
            # Muse's own inbox: results and messages addressed to the user.
            where = "m.to_agent_id IS NULL"
            key = None
        if unread_only:
            where += " AND m.read_at IS NULL"
        rows = self.db.query(
            "SELECT m.*, COALESCE(f.name, 'User') AS from_name, "
            "t.name AS to_name, ta.title AS task_title "
            "FROM agent_messages m "
            "LEFT JOIN agents f ON f.id = m.from_agent_id "
            "LEFT JOIN agents t ON t.id = m.to_agent_id "
            "LEFT JOIN agent_tasks ta ON ta.id = m.task_id "
            "WHERE " + where + " ORDER BY m.id DESC LIMIT ?",
            (key, max(1, min(int(limit), 100))) if key is not None
            else (max(1, min(int(limit), 100)),),
        )
        if mark_read and rows:
            ids = [int(row["id"]) for row in rows if not row["read_at"]]
            if ids:
                marks = ",".join("?" for _ in ids)
                self.db.execute(
                    "UPDATE agent_messages SET read_at = ? WHERE id IN (%s)" % marks,
                    tuple([time.time()] + ids),
                )
        return list(reversed(rows))

    def task_thread(self, task_id: int) -> List[Dict[str, Any]]:
        self.get_task(task_id)
        return self.db.query(
            "SELECT m.*, COALESCE(f.name, 'User') AS from_name, "
            "t.name AS to_name FROM agent_messages m "
            "LEFT JOIN agents f ON f.id = m.from_agent_id "
            "LEFT JOIN agents t ON t.id = m.to_agent_id "
            "WHERE m.task_id = ? ORDER BY m.id",
            (int(task_id),),
        )

    # -- group conversations ---------------------------------------------
    @staticmethod
    def group_key(agent_a: int, agent_b: int) -> Tuple[int, int, str]:
        first, second = sorted((int(agent_a), int(agent_b)))
        return first, second, "group:%d:%d" % (first, second)

    def list_groups(self, limit: int = 40) -> List[Dict[str, Any]]:
        pairs: List[Dict[str, Any]] = []
        for row in self.db.query(
            "SELECT CASE WHEN from_agent_id < to_agent_id "
            "THEN from_agent_id ELSE to_agent_id END AS agent_a, "
            "CASE WHEN from_agent_id < to_agent_id "
            "THEN to_agent_id ELSE from_agent_id END AS agent_b, "
            "MAX(id) AS last_id FROM agent_messages "
            "WHERE from_agent_id IS NOT NULL AND to_agent_id IS NOT NULL "
            "GROUP BY agent_a, agent_b"
        ):
            pairs.append(row)
        for row in self.db.query(
            "SELECT ? AS agent_a, to_agent_id AS agent_b, MAX(id) AS last_id "
            "FROM agent_messages WHERE from_agent_id IS NULL "
            "AND to_agent_id IS NOT NULL AND kind != 'group' "
            "GROUP BY to_agent_id",
            (MUSE_ID,),
        ):
            pairs.append(row)
        for row in self.db.query(
            "SELECT ? AS agent_a, from_agent_id AS agent_b, MAX(id) AS last_id "
            "FROM agent_messages WHERE from_agent_id IS NOT NULL "
            "AND to_agent_id IS NULL AND kind != 'group' "
            "GROUP BY from_agent_id",
            (MUSE_ID,),
        ):
            pairs.append(row)
        pairs.sort(key=lambda item: int(item["last_id"]), reverse=True)
        groups: List[Dict[str, Any]] = []
        for row in pairs[: max(1, min(int(limit), 100))]:
            agent_a_id = int(row["agent_a"])
            agent_b_id = int(row["agent_b"])
            first = (
                {"id": MUSE_ID, "name": self.lead_name(), "status": "active"}
                if agent_a_id == MUSE_ID
                else self.db.one(
                    "SELECT id, name, status FROM agents WHERE id = ?",
                    (agent_a_id,),
                )
            )
            second = self.db.one(
                "SELECT id, name, status FROM agents WHERE id = ?",
                (agent_b_id,),
            )
            last = self.db.one(
                "SELECT content, created_at, from_agent_id FROM agent_messages "
                "WHERE id = ?",
                (int(row["last_id"]),),
            )
            if not first or not second or not last:
                continue
            content = last["content"]
            updated_at = last["created_at"]
            if agent_a_id == MUSE_ID:
                task = self.db.one(
                    "SELECT title, result, error, finished_at, created_at "
                    "FROM agent_tasks WHERE agent_id = ? "
                    "AND created_by_agent_id IS NULL "
                    "AND status IN ('done','needs_approval','failed') "
                    "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1",
                    (agent_b_id,),
                )
                task_at = (
                    float(task["finished_at"] or task["created_at"]) if task else 0.0
                )
                if task and task_at > float(updated_at):
                    content = task["result"] or task["error"] or task["title"]
                    updated_at = task_at
            groups.append(
                {
                    "id": "%d:%d" % (first["id"], second["id"]),
                    "agent_a_id": first["id"],
                    "agent_b_id": second["id"],
                    "agent_a_name": first["name"],
                    "agent_b_name": second["name"],
                    "title": "%s ↔ %s" % (first["name"], second["name"]),
                    "last_message": _clean(content, 180),
                    "last_from_agent_id": last["from_agent_id"],
                    "updated_at": updated_at,
                }
            )
        return groups

    def group_info(self, agent_a: int, agent_b: int) -> Dict[str, Any]:
        first, second, _ = self.group_key(agent_a, agent_b)
        a = (
            {"id": MUSE_ID, "name": self.lead_name(), "status": "active"}
            if first == MUSE_ID
            else self.get_agent(first)
        )
        b = self.get_agent(second)
        return {
            "id": "%d:%d" % (first, second),
            "agent_a_id": a["id"],
            "agent_b_id": b["id"],
            "agent_a_name": a["name"],
            "agent_b_name": b["name"],
            "title": "%s ↔ %s" % (a["name"], b["name"]),
        }

    def group_messages(
        self, agent_a: int, agent_b: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        first, second, chat_id = self.group_key(agent_a, agent_b)
        second_agent = self.get_agent(second)
        messages: List[Dict[str, Any]] = []
        if first == MUSE_ID:
            agent_rows = self.db.query(
                "SELECT m.*, f.name AS from_name, t.name AS to_name "
                "FROM agent_messages m "
                "LEFT JOIN agents f ON f.id = m.from_agent_id "
                "LEFT JOIN agents t ON t.id = m.to_agent_id "
                "WHERE ((m.from_agent_id IS NULL AND m.to_agent_id = ?) "
                "OR (m.from_agent_id = ? AND m.to_agent_id IS NULL)) "
                "AND m.kind != 'group' ORDER BY m.id",
                (second, second),
            )
            task_rows = self.db.query(
                "SELECT id, title, result, error, status, created_at, finished_at "
                "FROM agent_tasks WHERE agent_id = ? "
                "AND created_by_agent_id IS NULL "
                "AND status IN ('done','needs_approval','failed') ORDER BY id",
                (second,),
            )
        else:
            agent_rows = self.db.query(
                "SELECT m.*, f.name AS from_name, t.name AS to_name "
                "FROM agent_messages m "
                "LEFT JOIN agents f ON f.id = m.from_agent_id "
                "LEFT JOIN agents t ON t.id = m.to_agent_id "
                "WHERE m.from_agent_id IS NOT NULL AND m.to_agent_id IS NOT NULL "
                "AND ((m.from_agent_id = ? AND m.to_agent_id = ?) "
                "OR (m.from_agent_id = ? AND m.to_agent_id = ?)) "
                "ORDER BY m.id",
                (first, second, second, first),
            )
            task_rows = []
        user_rows = self.db.query(
            "SELECT id, content, created_at FROM messages "
            "WHERE chat_id = ? AND role = 'user' ORDER BY id",
            (chat_id,),
        )
        for row in agent_rows:
            messages.append(
                {
                    "role": "assistant",
                    "content": row["content"],
                    "from_agent_id": row["from_agent_id"],
                    "from_name": (
                        self.lead_name()
                        if row["from_agent_id"] is None
                        else row.get("from_name") or "Agent"
                    ),
                    "to_agent_id": row["to_agent_id"],
                    "to_name": row.get("to_name") or second_agent["name"],
                    "kind": row["kind"],
                    "created_at": row["created_at"],
                }
            )
        for row in task_rows:
            content = row["result"] or row["error"] or row["title"]
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "from_agent_id": second,
                    "from_name": second_agent["name"],
                    "to_agent_id": MUSE_ID,
                    "to_name": self.lead_name(),
                    "kind": "task_result",
                    "task_id": row["id"],
                    "created_at": row["finished_at"] or row["created_at"],
                }
            )
        for row in user_rows:
            messages.append(
                {
                    "role": "user",
                    "content": row["content"],
                    "from_name": "You",
                    "created_at": row["created_at"],
                }
            )
        messages.sort(key=lambda item: item["created_at"])
        return messages[-max(1, min(int(limit), 300)):]

    def send_group_message(self, agent_a: int, agent_b: int, content: str) -> Dict[str, Any]:
        first, second, chat_id = self.group_key(agent_a, agent_b)
        content = _clean(content, 8000)
        if not content:
            raise TeamError("Message cannot be empty.")
        first_agent = (
            {"id": MUSE_ID, "name": self.lead_name()}
            if first == MUSE_ID
            else self.get_agent(first)
        )
        second_agent = self.get_agent(second)
        now = time.time()
        message_id = self.db.insert(
            "INSERT INTO messages (chat_id, role, content, attachments_json, created_at) "
            "VALUES (?, 'user', ?, '[]', ?)",
            (chat_id, content, now),
        )
        recipients = [second_agent] if first == MUSE_ID else [first_agent, second_agent]
        for agent in recipients:
            self.db.insert(
                "INSERT INTO agent_messages (from_agent_id, to_agent_id, task_id, "
                "kind, content, created_at) VALUES (NULL, ?, NULL, 'group', ?, ?)",
                (agent["id"], content, now),
            )
        self.audit.log(
            "user",
            "agent_group_message",
            {
                "message_id": message_id,
                "agents": [agent["id"] for agent in recipients],
            },
        )
        return {
            "id": message_id,
            "content": content,
            "created_at": now,
            "group": self.group_info(first, second),
        }


    def snapshot(self) -> Dict[str, Any]:
        agents = self.list_agents()
        tasks = self.list_tasks(limit=60)
        messages = self.db.query(
            "SELECT m.*, COALESCE(f.name, 'User') AS from_name, "
            "t.name AS to_name FROM agent_messages m "
            "LEFT JOIN agents f ON f.id = m.from_agent_id "
            "LEFT JOIN agents t ON t.id = m.to_agent_id "
            "ORDER BY m.id DESC LIMIT 40"
        )
        return {
            "agents": agents,
            "tasks": tasks,
            "messages": list(reversed(messages)),
            "limits": {
                "max_agents": self.settings.team_max_agents,
                "max_depth": self.settings.team_max_depth,
                "auto_run": bool(self.settings.team_auto_run),
            },
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "agents": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM agents WHERE status != 'archived'"
                )
                or 0
            ),
            "queued": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM agent_tasks WHERE status = 'queued'"
                )
                or 0
            ),
            "working": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM agent_tasks WHERE status = 'working'"
                )
                or 0
            ),
            "needs_approval": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM agent_tasks "
                    "WHERE status = 'needs_approval'"
                )
                or 0
            ),
        }


class TeamRunner:
    """Runs queued team tasks against their assigned named agent."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self._worker: Optional[asyncio.Task] = None
        self._tasks: Dict[int, asyncio.Task] = {}
        self._active: set[int] = set()
        self._stopping = False

    def start(self) -> None:
        if self._worker and not self._worker.done():
            return
        if not self.services.settings.team_enabled:
            return
        self.services.team.recover_working()
        if not self.services.settings.team_auto_run:
            return
        self._stopping = False
        self._worker = asyncio.create_task(self._loop(), name="muse-team-runner")

    async def stop(self) -> None:
        self._stopping = True
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        running = list(self._active)
        for task_id in running:
            self.services.team.requeue_task(task_id)
        self._active.clear()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - background best effort
                logger.exception("Team runner tick failed")
            try:
                await asyncio.sleep(max(0.5, float(self.services.settings.team_tick_seconds)))
            except asyncio.CancelledError:
                raise

    def tick(self) -> int:
        if not self.services.settings.team_enabled:
            return 0
        started = 0
        limit = max(1, int(self.services.settings.team_max_concurrent))
        while len(self._active) < limit:
            task = self.services.team.claim_next()
            if not task:
                break
            self._spawn(int(task["id"]))
            started += 1
        return started

    def _spawn(self, task_id: int) -> None:
        self._active.add(int(task_id))

        async def run() -> None:
            try:
                await self._execute(int(task_id))
            except asyncio.CancelledError:
                self.services.team.requeue_task(int(task_id))
                raise
            except Exception:
                logger.exception("Team task %s crashed", task_id)
                try:
                    self.services.team.fail_task(int(task_id), "Runner crashed.")
                except Exception:
                    logger.exception("Could not mark team task failed")
            finally:
                self._active.discard(int(task_id))

        spawned = asyncio.create_task(run(), name="muse-team-task-%d" % task_id)
        self._tasks[int(task_id)] = spawned
        spawned.add_done_callback(
            lambda _task, key=int(task_id): self._tasks.pop(key, None)
        )

    async def run_task(self, task_id: int) -> Dict[str, Any]:
        task = self.services.team.get_task(task_id)
        if task["status"] not in ("queued", "needs_approval"):
            raise TeamError("Task is not queued or waiting for approval.")
        claimed = self.services.team.claim_task(task_id)
        if not claimed:
            raise TeamError("Task was claimed by the team runner. Try again.")
        self._active.add(int(task_id))
        current = asyncio.current_task()
        if current is not None:
            self._tasks[int(task_id)] = current
        try:
            await self._execute(int(task_id))
        finally:
            self._tasks.pop(int(task_id), None)
            self._active.discard(int(task_id))
        return self.services.team.get_task(task_id)


    def _task_prompt(
        self,
        task: Dict[str, Any],
        agent: Dict[str, Any],
        inbox: List[Dict[str, Any]],
    ) -> str:
        roster = "\n".join(
            "- #%s %s — %s" % (item["id"], item["name"], item["role"])
            for item in self.services.team.list_agents()
            if item["id"] != agent["id"]
        ) or "- (no other agents yet)"
        messages = "\n".join(
            "- %s: %s" % (item.get("from_name") or "User", item["content"][:1200])
            for item in inbox
        ) or "- (empty)"
        parent = ""
        if task.get("parent_task_id"):
            parent = "Parent task: #%s\n" % task["parent_task_id"]
        return (
            "You are %s, %s.\n"
            "Standing instructions:\n%s\n\n"
            "Team roster (use #id or the exact name when delegating):\n%s\n\n"
            "Your inbox:\n%s\n\n"
            "%sTask #%s: %s\n%s\n\n"
            "Work autonomously now. Use assign_task to hand a real sub-task "
            "to another specialist, send_message to coordinate, and "
            "read_inbox to check replies. Do not assign this task back to "
            "yourself. When finished, reply with the finished result, not a "
            "plan or a question, unless you genuinely need a human decision."
            % (
                agent["name"],
                agent.get("role") or "teammate",
                agent.get("instructions") or agent.get("role") or "",
                roster,
                messages,
                parent,
                task["id"],
                task["title"],
                task["prompt"],
            )
        )

    async def _execute(self, task_id: int) -> None:
        task = self.services.team.get_task(task_id)
        if task["status"] == "cancelled":
            return
        try:
            agent = self.services.team.get_agent(task["agent_id"])
        except AgentNotFound:
            self.services.team.fail_task(task_id, "Assigned agent no longer exists.")
            return
        if agent["status"] != "active":
            self.services.team.fail_task(
                task_id, "Assigned agent is %s." % agent["status"]
            )
            return
        inbox = self.services.team.inbox(
            agent["id"], limit=12, unread_only=True, mark_read=True
        )
        prompt = self._task_prompt(task, agent, inbox)
        from .agent import Agent  # local import avoids a module cycle

        runner_agent = Agent(self.services, profile=agent)
        self.services.audit.log(
            "agent:%s" % agent["id"],
            "team_task_started",
            {
                "task_id": int(task_id),
                "agent_id": int(agent["id"]),
                "model": agent.get("model") or "",
            },
        )
        try:
            timeout = float(self.services.settings.agent_run_timeout_seconds)
            chat_id = "agent:%d:task:%d" % (agent["id"], task_id)
            if timeout > 0:
                result = await asyncio.wait_for(
                    runner_agent.run(
                        chat_id=chat_id,
                        user_text=prompt,
                        actor="agent:%d" % agent["id"],
                    ),
                    timeout=timeout,
                )
            else:
                result = await runner_agent.run(
                    chat_id=chat_id,
                    user_text=prompt,
                    actor="agent:%d" % agent["id"],
                )
        except asyncio.TimeoutError:
            self.services.team.fail_task(
                task_id,
                "Run exceeded %d seconds and was stopped."
                % int(self.services.settings.agent_run_timeout_seconds),
            )
            return
        except asyncio.CancelledError:
            self.services.team.requeue_task(task_id)
            raise
        except Exception as exc:
            self.services.team.fail_task(
                task_id, "%s: %s" % (type(exc).__name__, exc)
            )
            return
        reply = str(result.get("reply") or "No reply.")
        if result.get("pending_approvals") or result.get("pending_credentials"):
            self.services.team.block_task(
                task_id,
                reply,
                reason="The agent is waiting for approval or a credential.",
            )
            return
        self.services.team.complete_task(task_id, reply)

