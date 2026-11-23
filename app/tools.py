"""Agent tool definitions and the approval / permission gate.

Read tools run directly. Routine browser clicks, typing and key presses are
auto-approved by default; other write tools (scheduling, paid image generation)
require user approval unless ``auto_approve_writes`` is on. Shell commands run
directly for normal workspace work; install commands, unsafe paths, and non-GET
downloads require approval and are remembered after approval. ``vault_fill``
always requires approval and never exposes secret values to the model.
``run_shell`` can read anything the container user can read, so treat it as
powerful.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .services import Services
from .shell import (
    ShellError,
    run_shell_command,
    shell_approval_reason,
    shell_command_family,
    shell_command_signature,
)
from .team import parse_chat_context
from .vault import host_matches

TOOL_TIMEOUT_SECONDS = 180
BROWSER_WRITE_TOOLS = {
    "browser_click",
    "browser_type",
    "browser_press",
    "vault_fill",
}
BROWSER_ROUTINE_TOOLS = {"browser_click", "browser_type", "browser_press"}


def _browser_session(chat_id: str) -> str:
    agent_id, _ = parse_chat_context(chat_id)
    return "agent:%d" % agent_id if agent_id else "default"


def _current_host(services: Services, chat_id: str = "default") -> str:
    status = services.browser.status(_browser_session(chat_id))
    url = status.get("url") or ""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    return host.lower()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any], str], Awaitable[Any]]
    side_effect: bool = False
    always_approve: bool = False

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _str(description: str, enum: Optional[List[str]] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {"type": "string", "description": description}
    if enum:
        data["enum"] = enum
    return data


def _require_text(args: Dict[str, Any], key: str, max_len: int = 8000) -> str:
    value = str(args.get(key, "")).strip()
    if not value:
        raise ValueError("%s is required." % key)
    return value[:max_len]


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class ToolKit:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.specs = self._build()

    # -- helpers ----------------------------------------------------------
    def _artifact(self, kind: str, filename: str, prompt: str) -> int:
        return self.services.db.insert(
            "INSERT INTO artifacts (kind, filename, prompt, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, filename, prompt[:1000], time.time()),
        )

    def schemas(self) -> List[Dict[str, Any]]:
        return [spec.schema() for spec in self.specs.values()]

    # -- content generation ----------------------------------------------
    async def generate_text(self, args: Dict[str, Any], chat_id: str) -> Any:
        prompt = _require_text(args, "prompt")
        max_words = _clamp_int(args.get("max_words"), 0, 30, 3000) or None
        text = await self.services.openai.agenerate_text(
            prompt, max_words=max_words, session_id=chat_id
        )
        filename = "text-%s.md" % uuid.uuid4().hex
        path = self.services.settings.artifacts_dir / filename
        path.write_text(text, encoding="utf-8")
        artifact_id = self._artifact("text", filename, prompt)
        return {
            "text": text[:12000],
            "artifact_id": artifact_id,
            "url": "/api/artifacts/%d/raw" % artifact_id,
        }

    async def generate_image(self, args: Dict[str, Any], chat_id: str) -> Any:
        prompt = _require_text(args, "prompt")
        size = str(args.get("size") or "").strip() or None
        saved = await self.services.openai.agenerate_image(prompt, size)
        artifact_id = self._artifact("image", saved["filename"], prompt)
        return {
            "artifact_id": artifact_id,
            "image_url": "/api/artifacts/%d/raw" % artifact_id,
            "filename": saved["filename"],
        }

    # -- scheduling -------------------------------------------------------
    async def schedule_task(self, args: Dict[str, Any], chat_id: str) -> Any:
        title = _require_text(args, "title", 120)
        prompt = _require_text(args, "prompt")
        kind = str(args.get("kind", "")).strip()
        schedule = str(args.get("schedule", "")).strip()
        timezone = str(args.get("timezone") or "").strip() or None
        task = self.services.scheduler.create(title, prompt, kind, schedule, timezone)
        return {
            "task_id": task["id"],
            "title": task["title"],
            "kind": task["kind"],
            "next_run_at": task["next_run_at"],
        }

    async def list_tasks(self, args: Dict[str, Any], chat_id: str) -> Any:
        tasks = self.services.scheduler.list_tasks()
        return {
            "tasks": [
                {
                    "id": task["id"],
                    "title": task["title"],
                    "kind": task["kind"],
                    "schedule": task["schedule"],
                    "timezone": task["timezone"],
                    "active": bool(task["active"]),
                    "next_run_at": task["next_run_at"],
                    "last_status": task["last_status"],
                }
                for task in tasks[:40]
            ]
        }

    async def cancel_task(self, args: Dict[str, Any], chat_id: str) -> Any:
        task_id = _clamp_int(args.get("task_id"), 0, 1, 10**9)
        if not task_id:
            raise ValueError("task_id is required.")
        self.services.scheduler.delete(task_id)
        return {"deleted_task_id": task_id}

    # -- memory -----------------------------------------------------------
    async def remember(self, args: Dict[str, Any], chat_id: str) -> Any:
        fact = _require_text(args, "fact", 1000)
        self.services.db.execute(
            "INSERT INTO memories (content, source, created_at) VALUES (?, ?, ?)",
            (fact, chat_id, time.time()),
        )
        return {"stored": True}

    async def recall(self, args: Dict[str, Any], chat_id: str) -> Any:
        query = str(args.get("query", "")).strip()[:200]
        if query:
            rows = self.services.db.query(
                "SELECT content, created_at FROM memories WHERE content LIKE ? "
                "ORDER BY id DESC LIMIT 10",
                ("%" + query + "%",),
            )
        else:
            rows = self.services.db.query(
                "SELECT content, created_at FROM memories ORDER BY id DESC LIMIT 10"
            )
        return {"memories": [row["content"] for row in rows]}

    # -- shell ------------------------------------------------------------
    async def run_shell(self, args: Dict[str, Any], chat_id: str) -> Any:
        if not bool(self.services.prefs.get("shell_enabled")):
            return {
                "error": (
                    "Shell access is disabled. Enable it in Settings; normal "
                    "workspace commands then run directly, while installs and "
                    "unsafe downloads need approval."
                )
            }
        command = _require_text(args, "command", 12_000)
        cwd = str(args.get("cwd") or "").strip()[:500]
        timeout = _clamp_int(
            args.get("timeout_seconds"),
            int(self.services.settings.shell_timeout_seconds),
            1,
            120,
        )
        try:
            return await run_shell_command(
                self.services.settings,
                command,
                cwd=cwd,
                timeout_seconds=timeout,
            )
        except ShellError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}

    # -- browser ----------------------------------------------------------
    async def browser_open(self, args: Dict[str, Any], chat_id: str) -> Any:
        url = _require_text(args, "url", 2048)
        return await self.services.browser.open(
            url, session=_browser_session(chat_id)
        )

    async def browser_read(self, args: Dict[str, Any], chat_id: str) -> Any:
        max_chars = _clamp_int(args.get("max_chars"), 6000, 200, 20_000)
        return await self.services.browser.read(
            max_chars, session=_browser_session(chat_id)
        )

    async def browser_screenshot(self, args: Dict[str, Any], chat_id: str) -> Any:
        session = _browser_session(chat_id)
        status = self.services.browser.status(session)
        await self.services.browser.screenshot(session)  # force a fresh render
        return {
            "url": status["url"],
            "session": session,
            "screenshot_url": "/api/browser/screenshot?session="
            + session,
            "note": "The user can watch this page in the Browser tab.",
        }

    async def browser_click(self, args: Dict[str, Any], chat_id: str) -> Any:
        target = _require_text(args, "target", 500)
        return await self.services.browser.click(
            target, session=_browser_session(chat_id)
        )

    async def browser_type(self, args: Dict[str, Any], chat_id: str) -> Any:
        selector = _require_text(args, "selector", 500)
        text = str(args.get("text", ""))[:4000]
        submit = bool(args.get("submit", False))
        return await self.services.browser.type_text(
            selector, text, submit, session=_browser_session(chat_id)
        )

    async def browser_press(self, args: Dict[str, Any], chat_id: str) -> Any:
        key = _require_text(args, "key", 40)
        return await self.services.browser.press(
            key, session=_browser_session(chat_id)
        )

    # -- vault ------------------------------------------------------------
    async def vault_list(self, args: Dict[str, Any], chat_id: str) -> Any:
        status = self.services.vault.status()
        return {
            "initialized": status["initialized"],
            "unlocked": status["unlocked"],
            "entries": [
                {"label": entry["label"], "domains": entry["domains"]}
                for entry in self.services.vault.list_entries()
            ],
        }

    async def vault_fill(self, args: Dict[str, Any], chat_id: str) -> Any:
        label = str(args.get("label") or "").strip()[:80]
        username_selector = str(args.get("username_selector", ""))[:500]
        password_selector = str(args.get("password_selector", ""))[:500]
        submit_selector = str(args.get("submit_selector", ""))[:500]
        result = await self.services.browser.fill_credential(
            label,
            username_selector=username_selector,
            password_selector=password_selector,
            submit_selector=submit_selector,
            session=_browser_session(chat_id),
        )
        if result.get("otp_required") and result.get("otp_selector"):
            url = str(result.get("url") or "")
            domain = (urlparse(url).hostname or "").lower()
            if domain:
                request = self.services.credentials.create(
                    chat_id,
                    str(result.get("entry_label") or domain)[:80],
                    domain,
                    url,
                    "Muse needs the one-time code shown by the site to continue "
                    "the login.",
                    submit_selector=str(result.get("otp_submit_selector") or "")[:500],
                    kind="otp",
                    otp_selector=str(result.get("otp_selector") or "")[:500],
                )
                result.update(
                    {
                        "status": "otp_required",
                        "request_id": request["id"],
                        "message": (
                            "The site is asking for a one-time code. A secure OTP "
                            "card is waiting in the UI. Do not retry; tell the "
                            "user to enter the code there."
                        ),
                    }
                )
        return result

    async def request_otp(self, args: Dict[str, Any], chat_id: str) -> Any:
        browser = self.services.browser
        session = _browser_session(chat_id)
        status = browser.status(session)
        if not status.get("started") or not status.get("url"):
            raise ValueError("Open the page that is asking for the code first.")
        fields = await browser.detect_otp_fields(session)
        if not fields.get("otp"):
            raise ValueError(
                "No visible one-time-code field was found on the current page."
            )
        url = str(fields.get("url") or status.get("url") or "")
        domain = (urlparse(url).hostname or "").lower()
        if not domain:
            raise ValueError("The current page has no hostname.")
        label = str(args.get("label") or "").strip()[:80] or domain
        reason = str(args.get("reason") or "").strip()[:1000] or (
            "Muse needs the one-time code shown by the site to continue. "
            "The code is submitted directly and never stored."
        )
        request = self.services.credentials.create(
            chat_id,
            label,
            domain,
            url,
            reason,
            submit_selector=str(fields.get("submit") or "")[:500],
            kind="otp",
            otp_selector=fields["otp"],
        )
        return {
            "status": "otp_required",
            "request_id": request["id"],
            "domain": domain,
            "label": request["label"],
            "message": (
                "A secure one-time-code card is waiting in the UI. Do not ask "
                "for the code in chat. Tell the user to enter it there and stop."
            ),
        }

    async def request_credential(self, args: Dict[str, Any], chat_id: str) -> Any:
        browser = self.services.browser
        session = _browser_session(chat_id)
        status = browser.status(session)
        if not status.get("started") or not status.get("url"):
            raise ValueError("Open the login page first with browser_open.")
        domain = (urlparse(status["url"]).hostname or "").lower()
        if not domain:
            raise ValueError("The current page has no hostname.")

        fields = await browser.detect_login_fields(session)
        existing = [
            {"label": entry["label"], "domains": entry["domains"]}
            for entry in self.services.vault.list_entries()
            if host_matches(domain, entry["domains"])
        ]
        if existing:
            return {
                "status": "existing_credentials",
                "domain": domain,
                "entries": existing,
                "message": (
                    "A vault entry already exists for this site. Use vault_fill "
                    "with one of these labels instead of creating a new request."
                ),
            }

        label = str(args.get("label") or "").strip()[:80] or domain
        reason = str(args.get("reason") or "").strip()[:1000]
        request = self.services.credentials.create(
            chat_id,
            label,
            domain,
            fields.get("url") or status["url"],
            reason,
            username_selector=str(
                args.get("username_selector") or fields.get("username") or ""
            )[:500],
            password_selector=str(
                args.get("password_selector") or fields.get("password") or ""
            )[:500],
            submit_selector=str(
                args.get("submit_selector") or fields.get("submit") or ""
            )[:500],
        )
        return {
            "status": "credential_required",
            "request_id": request["id"],
            "domain": domain,
            "label": request["label"],
            "login_url": request["login_url"],
            "message": (
                "A credential card is waiting in the UI. Do not ask for the "
                "password in chat. After the user saves it, Muse submits the "
                "login automatically and continues."
            ),
        }
    async def request_vault_entry(self, args: Dict[str, Any], chat_id: str) -> Any:
        """Ask the UI for a vault entry without automating a login."""
        raw = str(args.get("domain") or args.get("url") or "").strip()
        domain = (urlparse(raw).hostname or raw).lower().strip().rstrip(".")
        if not domain:
            status = self.services.browser.status(_browser_session(chat_id))
            domain = (urlparse(status.get("url") or "").hostname or "").lower()
        if not domain:
            raise ValueError(
                "Provide a domain (for example google.com) or open the site first."
            )
        label = str(args.get("label") or "").strip()[:80] or domain
        reason = str(args.get("reason") or "").strip()[:1000] or (
            "This site needs a credential. Add it securely below; never paste "
            "passwords into chat."
        )
        existing = [
            {"label": entry["label"], "domains": entry["domains"]}
            for entry in self.services.vault.list_entries()
            if host_matches(domain, entry["domains"])
        ]
        if existing:
            return {
                "status": "existing_credentials",
                "domain": domain,
                "entries": existing,
                "message": (
                    "A vault entry already exists for this site. The user can "
                    "unlock the vault and use it for domain-bound autofill."
                ),
            }
        request = self.services.credentials.create(
            chat_id,
            label,
            domain,
            "",
            reason,
            kind="vault",
        )
        return {
            "status": "credential_required",
            "mode": "vault",
            "request_id": request["id"],
            "domain": domain,
            "label": request["label"],
            "message": (
                "A vault card is waiting in the UI. The user adds the entry "
                "securely; this does not attempt to sign in or expose secrets "
                "to you."
            ),
        }

    # -- team -------------------------------------------------------------
    def _team_agent(self, chat_id: str) -> Optional[int]:
        agent_id, _ = parse_chat_context(chat_id)
        return agent_id

    def _resolve_team_agent(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = args.get("agent_id") or args.get("agent") or ""
        try:
            agent_id = int(raw)
        except (TypeError, ValueError):
            agent_id = 0
        if agent_id:
            return self.services.team.get_agent(agent_id)
        name = str(raw or "").strip()
        if not name:
            raise ValueError("agent_id or agent name is required.")
        return self.services.team.get_agent(name=name)

    async def list_team(self, args: Dict[str, Any], chat_id: str) -> Any:
        me = self._team_agent(chat_id)
        return {
            "me": me,
            "agents": [
                {
                    "id": agent["id"],
                    "name": agent["name"],
                    "role": agent["role"],
                    "status": agent["status"],
                    "open_tasks": agent["open_tasks"],
                    "done_tasks": agent["done_tasks"],
                }
                for agent in self.services.team.list_agents()
            ],
        }

    async def rename_self(self, args: Dict[str, Any], chat_id: str) -> Any:
        me = self._team_agent(chat_id)
        if not me:
            raise ValueError(
                "rename_self is only available to a named agent while it runs."
            )
        current = self.services.team.get_agent(me)
        name = str(args.get("name") or "").strip()[:60]
        role = str(args.get("role") or "").strip()[:200]
        if not name and not role:
            raise ValueError("Provide a new name, a new role, or both.")
        if "name" in args and not name:
            raise ValueError("name cannot be empty.")
        if "role" in args and not role:
            raise ValueError("role cannot be empty.")
        values: Dict[str, Any] = {}
        if name:
            values["name"] = name
        if role:
            values["role"] = role
        agent = self.services.team.update_agent(
            me, values, actor="agent:%d" % me
        )
        return {
            "agent_id": me,
            "previous_name": current["name"],
            "name": agent["name"],
            "role": agent["role"],
            "message": (
                "Renamed yourself to %s. Your standing instructions and existing "
                "tasks are unchanged." % agent["name"]
            ),
        }

    async def create_agent(self, args: Dict[str, Any], chat_id: str) -> Any:
        brief = _require_text(args, "brief")
        name = str(args.get("name") or "").strip()[:60] or None
        role = str(args.get("role") or "").strip()[:200] or None
        me = self._team_agent(chat_id)
        created_by = "agent:%d" % me if me else "user"
        agent = await self.services.team.create_from_prompt(
            brief, name=name, role=role, created_by=created_by
        )
        return {
            "agent_id": agent["id"],
            "name": agent["name"],
            "role": agent["role"],
            "status": agent["status"],
        }

    async def assign_task(self, args: Dict[str, Any], chat_id: str) -> Any:
        agent = self._resolve_team_agent(args)
        title = _require_text(args, "title", 120)
        prompt = _require_text(args, "prompt")
        me, current_task = parse_chat_context(chat_id)
        if me and int(agent["id"]) == int(me):
            raise ValueError("Do not assign a task back to yourself.")
        parent = args.get("parent_task_id")
        try:
            parent_id = int(parent) if parent else current_task
        except (TypeError, ValueError):
            parent_id = current_task
        actor = "agent:%d" % me if me else "user"
        task = self.services.team.create_task(
            title,
            prompt,
            int(agent["id"]),
            created_by_agent_id=me,
            parent_task_id=parent_id,
            actor=actor,
        )
        return {
            "task_id": task["id"],
            "title": task["title"],
            "agent": task["agent_name"],
            "status": task["status"],
            "parent_task_id": task["parent_task_id"],
            "message": "Task queued for %s." % task["agent_name"],
        }


    async def send_team_message(self, args: Dict[str, Any], chat_id: str) -> Any:
        agent = self._resolve_team_agent(args)
        content = _require_text(args, "content")
        me, current_task = parse_chat_context(chat_id)
        task_id = args.get("task_id") or current_task
        try:
            task_id = int(task_id) if task_id else None
        except (TypeError, ValueError):
            task_id = current_task
        message = self.services.team.send_message(
            from_agent_id=me,
            to_agent_id=int(agent["id"]),
            content=content,
            task_id=task_id,
            kind="message",
            actor="agent:%d" % me if me else "user",
        )
        return {
            "message_id": message["id"],
            "to": message["to_name"],
            "task_id": message["task_id"],
        }

    async def read_inbox(self, args: Dict[str, Any], chat_id: str) -> Any:
        me = self._team_agent(chat_id)
        if not me:
            raw = args.get("agent_id") or args.get("agent")
            if raw:
                me = int(self._resolve_team_agent({"agent": raw})["id"])
        if not me:
            raise ValueError("read_inbox is only available while an agent runs.")
        rows = self.services.team.inbox(
            me,
            limit=_clamp_int(args.get("limit"), 10, 1, 50),
            unread_only=bool(args.get("unread_only", False)),
            mark_read=bool(args.get("mark_read", True)),
        )
        return {
            "agent_id": me,
            "messages": [
                {
                    "from": row.get("from_name") or "User",
                    "task_id": row.get("task_id"),
                    "kind": row.get("kind"),
                    "content": row["content"][:3000],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    async def list_team_tasks(self, args: Dict[str, Any], chat_id: str) -> Any:
        raw = args.get("agent_id") or args.get("agent")
        agent_id = None
        if raw:
            agent_id = int(self._resolve_team_agent({"agent": raw})["id"])
        status = str(args.get("status") or "").strip() or None
        if status and status not in {
            "queued",
            "working",
            "needs_approval",
            "done",
            "failed",
            "cancelled",
        }:
            raise ValueError("Unknown task status.")
        rows = self.services.team.list_tasks(
            limit=_clamp_int(args.get("limit"), 20, 1, 100),
            agent_id=agent_id,
            status=status,
        )
        return {
            "tasks": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "agent": row.get("agent_name"),
                    "status": row["status"],
                    "parent_task_id": row["parent_task_id"],
                    "result": (row.get("result") or "")[:1000],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }


    # -- registry ---------------------------------------------------------
    def _build(self) -> Dict[str, ToolSpec]:
        specs: Dict[str, ToolSpec] = {}
        specs.update(self._content_specs())
        specs.update(self._task_specs())
        specs.update(self._team_specs())
        specs.update(self._browser_specs())
        specs.update(self._shell_specs())
        return specs

    def _team_specs(self) -> Dict[str, ToolSpec]:
        return {
            "list_team": ToolSpec(
                name="list_team",
                description=(
                    "List the named agents on this team with their ids, roles, "
                    "status and task counts. Use this before delegating."
                ),
                parameters=_obj({}),
                handler=self.list_team,
            ),
            "rename_self": ToolSpec(
                name="rename_self",
                description=(
                    "Rename yourself or update your own one-line role when the "
                    "user explicitly asks. This changes only your own agent "
                    "profile label and role; your standing instructions, tasks "
                    "and identity id stay the same. It cannot rename other agents."
                ),
                parameters=_obj(
                    {
                        "name": _str("New display name for yourself."),
                        "role": _str("New one-line role for yourself."),
                    }
                ),
                handler=self.rename_self,
            ),
            "create_agent": ToolSpec(
                name="create_agent",
                description=(
                    "Create a new named specialist from a plain-language brief. "
                    "Muse designs the name, role and standing instructions. The "
                    "new agent can receive tasks. Creation is audited and capped "
                    "by TEAM_MAX_AGENTS."
                ),
                parameters=_obj(
                    {
                        "brief": _str(
                            "What this agent owns, what it should produce, and "
                            "what it should not do."
                        ),
                        "name": _str("Optional exact name."),
                        "role": _str("Optional one-line role override."),
                    },
                    ["brief"],
                ),
                handler=self.create_agent,
            ),
            "assign_task": ToolSpec(
                name="assign_task",
                description=(
                    "Assign a durable task to another named agent. The team "
                    "runner picks it up and the agent works independently; its "
                    "result comes back as a message. Use a clear title and a "
                    "complete prompt. Never assign a task back to yourself."
                ),
                parameters=_obj(
                    {
                        "agent": _str("Target agent id or exact name."),
                        "title": _str("Short task title."),
                        "prompt": _str("Complete instructions and expected output."),
                        "parent_task_id": {
                            "type": "integer",
                            "description": "Optional parent team task id.",
                        },
                    },
                    ["agent", "title", "prompt"],
                ),
                handler=self.assign_task,
            ),
            "send_message": ToolSpec(
                name="send_message",
                description=(
                    "Send a short coordination message to another named agent. "
                    "It appears in that agent's inbox and in the shared thread."
                ),
                parameters=_obj(
                    {
                        "agent": _str("Target agent id or exact name."),
                        "content": _str("Message body."),
                        "task_id": {
                            "type": "integer",
                            "description": "Optional related team task id.",
                        },
                    },
                    ["agent", "content"],
                ),
                handler=self.send_team_message,
            ),
            "read_inbox": ToolSpec(
                name="read_inbox",
                description=(
                    "Read messages sent to you by other agents or the user. "
                    "Use this at the start of a task and after delegating."
                ),
                parameters=_obj(
                    {
                        "unread_only": {"type": "boolean"},
                        "mark_read": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    }
                ),
                handler=self.read_inbox,
            ),
            "list_team_tasks": ToolSpec(
                name="list_team_tasks",
                description=(
                    "List the shared team task board with statuses, assignees "
                    "and recent results."
                ),
                parameters=_obj(
                    {
                        "agent": _str("Optional agent id or name filter."),
                        "status": _str(
                            "Optional status filter.",
                            [
                                "queued",
                                "working",
                                "needs_approval",
                                "done",
                                "failed",
                                "cancelled",
                            ],
                        ),
                        "limit": {"type": "integer"},
                    }
                ),
                handler=self.list_team_tasks,
            ),
        }

    def _content_specs(self) -> Dict[str, ToolSpec]:
        return {
            "generate_text": ToolSpec(
                name="generate_text",
                description=(
                    "Write text (draft, summary, email body, plan, article) with "
                    "a dedicated model call. The result is saved as a text "
                    "artifact. Use this for focused long-form writing; normal "
                    "chat replies are written by you directly."
                ),
                parameters=_obj(
                    {
                        "prompt": _str("What to write, including audience and tone."),
                        "max_words": {
                            "type": "integer",
                            "description": "Optional upper bound, 30-3000 words.",
                        },
                    },
                    ["prompt"],
                ),
                handler=self.generate_text,
            ),
            "generate_image": ToolSpec(
                name="generate_image",
                description=(
                    "Generate an image from a prompt with the configured image "
                    "model. Returns an image URL the user can see in chat. This "
                    "costs money and therefore asks for approval by default."
                ),
                parameters=_obj(
                    {
                        "prompt": _str("Detailed image prompt."),
                        "size": _str("Optional size such as 1024x1024."),
                    },
                    ["prompt"],
                ),
                handler=self.generate_image,
                side_effect=True,
            ),
            "remember": ToolSpec(
                name="remember",
                description=(
                    "Store a durable fact about the user (preferences, recurring "
                    "context). Never store secrets or passwords."
                ),
                parameters=_obj({"fact": _str("The fact to remember.")}, ["fact"]),
                handler=self.remember,
            ),
            "recall": ToolSpec(
                name="recall",
                description="Search stored facts about the user.",
                parameters=_obj({"query": _str("Search terms; empty = recent.")}),
                handler=self.recall,
            ),
        }

    def _task_specs(self) -> Dict[str, ToolSpec]:
        return {
            "schedule_task": ToolSpec(
                name="schedule_task",
                description=(
                    "Create a durable scheduled or recurring task. kind='once' "
                    "takes an ISO datetime like 2026-12-01T09:30, kind='interval' "
                    "takes seconds or values like 30m/2h/1d (minimum 60s), "
                    "kind='cron' takes a 5-field cron expression such as "
                    "'0 8 * * 1-5'. The prompt is sent to you when the task fires."
                ),
                parameters=_obj(
                    {
                        "title": _str("Short task title."),
                        "prompt": _str("Instruction to run at fire time."),
                        "kind": _str("Schedule type.", ["once", "interval", "cron"]),
                        "schedule": _str("Datetime, duration or cron expression."),
                        "timezone": _str("Optional IANA timezone, e.g. Europe/Berlin."),
                    },
                    ["title", "prompt", "kind", "schedule"],
                ),
                handler=self.schedule_task,
                side_effect=True,
            ),
            "list_tasks": ToolSpec(
                name="list_tasks",
                description="List scheduled tasks with their next run time.",
                parameters=_obj({}),
                handler=self.list_tasks,
            ),
            "cancel_task": ToolSpec(
                name="cancel_task",
                description="Delete a scheduled task by id.",
                parameters=_obj(
                    {"task_id": {"type": "integer", "description": "Task id."}},
                    ["task_id"],
                ),
                handler=self.cancel_task,
                side_effect=True,
            ),
        }

    def _browser_specs(self) -> Dict[str, ToolSpec]:
        return {
            "browser_open": ToolSpec(
                name="browser_open",
                description=(
                    "Open an http(s) URL in the sandboxed browser. Public sites "
                    "only; private networks are blocked. Returns page title and "
                    "visible text."
                ),
                parameters=_obj({"url": _str("Absolute http/https URL.")}, ["url"]),
                handler=self.browser_open,
            ),
            "browser_read": ToolSpec(
                name="browser_read",
                description="Read the visible text of the current page.",
                parameters=_obj(
                    {
                        "max_chars": {
                            "type": "integer",
                            "description": "Maximum characters (200-20000).",
                        }
                    }
                ),
                handler=self.browser_read,
            ),
            "browser_screenshot": ToolSpec(
                name="browser_screenshot",
                description=(
                    "Refresh and expose a screenshot of the current page. The "
                    "user can watch it live in the Browser tab."
                ),
                parameters=_obj({}),
                handler=self.browser_screenshot,
            ),
            "browser_click": ToolSpec(
                name="browser_click",
                description=(
                    "Click an element by CSS selector, or by its visible text if "
                    "the selector does not match. This changes page state and "
                    "needs approval."
                ),
                parameters=_obj(
                    {"target": _str("CSS selector or visible text.")}, ["target"]
                ),
                handler=self.browser_click,
                side_effect=True,
            ),
            "browser_type": ToolSpec(
                name="browser_type",
                description=(
                    "Type into an input identified by CSS selector, optionally "
                    "pressing Enter. Needs approval."
                ),
                parameters=_obj(
                    {
                        "selector": _str("CSS selector of the input."),
                        "text": _str("Text to type."),
                        "submit": {"type": "boolean", "description": "Press Enter."},
                    },
                    ["selector", "text"],
                ),
                handler=self.browser_type,
                side_effect=True,
            ),
            "browser_press": ToolSpec(
                name="browser_press",
                description="Press a keyboard key such as Enter or PageDown.",
                parameters=_obj({"key": _str("Key name.")}, ["key"]),
                handler=self.browser_press,
                side_effect=True,
            ),
            "vault_list": ToolSpec(
                name="vault_list",
                description=(
                    "List vault entry labels and their allowed domains. Secret "
                    "values are never available to you."
                ),
                parameters=_obj({}),
                handler=self.vault_list,
            ),
            "request_credential": ToolSpec(
                name="request_credential",
                description=(
                    "Show the user a secure credential card for the current login "
                    "page. Call this only after browser_open on the login page and "
                    "only when vault_list has no entry for this domain. Never ask "
                    "for a password in chat. Muse detects the login fields, stores "
                    "the submitted credential in the vault, submits the login, and "
                    "continues automatically."
                ),
                parameters=_obj(
                    {
                        "label": _str("Optional label, e.g. GitHub. Defaults to the domain."),
                        "reason": _str("Short explanation shown on the card."),
                        "username_selector": _str("Optional CSS selector; auto-detected."),
                        "password_selector": _str("Optional CSS selector; auto-detected."),
                        "submit_selector": _str("Optional CSS selector; auto-detected."),
                    }
                ),
                handler=self.request_credential,
            ),
            "request_otp": ToolSpec(
                name="request_otp",
                description=(
                    "Ask the user for the one-time code currently required by "
                    "the browser page. Use this when a login or verification "
                    "screen shows an OTP/MFA/code field. It creates a secure card "
                    "in chat; the code is submitted directly to the page and is "
                    "never stored. Never ask for the code in chat."
                ),
                parameters=_obj(
                    {
                        "label": _str("Optional display label, e.g. GitHub."),
                        "reason": _str("Short explanation shown on the card."),
                    }
                ),
                handler=self.request_otp,
            ),
            "request_vault_entry": ToolSpec(
                name="request_vault_entry",
                description=(
                    "Ask the user to add a credential to the vault without "
                    "automating a login. Use this when the vault is locked, no "
                    "entry exists, or the user wants to supply access for a site "
                    "you are not signing into. It shows a secure vault card and "
                    "never exposes the password to you."
                ),
                parameters=_obj(
                    {
                        "domain": _str(
                            "Domain such as google.com; omit to use the current page."
                        ),
                        "label": _str("Optional display label."),
                        "reason": _str("Why the user needs to add this credential."),
                    }
                ),
                handler=self.request_vault_entry,
            ),
            "vault_fill": ToolSpec(
                name="vault_fill",
                description=(
                    "Type a vault credential into the current page. The entry must "
                    "be domain-bound to the current site, the vault must be unlocked "
                    "by the user, and this always needs approval. Omit label to use "
                    "the entry bound to the current hostname; omit selectors to "
                    "auto-detect the visible login fields. You never see the "
                    "username or password."
                ),
                parameters=_obj(
                    {
                        "label": _str(
                            "Optional vault label. Omit to use the host-bound entry."
                        ),
                        "username_selector": _str("Optional CSS selector; auto-detected."),
                        "password_selector": _str("Optional CSS selector; auto-detected."),
                        "submit_selector": _str("Optional CSS selector; auto-detected."),
                    }
                ),
                handler=self.vault_fill,
                side_effect=True,
                always_approve=True,
            ),
        }

    def _shell_specs(self) -> Dict[str, ToolSpec]:
        return {
            "run_shell": ToolSpec(
                name="run_shell",
                description=(
                    "Run a shell command inside Muse's workspace. Use this for "
                    "git, curl, Python or other command-line tools when the "
                    "browser UI is awkward or a repository/file operation is "
                    "needed. Commands start in the shell workspace, receive a "
                    "minimal sanitized environment, and are killed after the "
                    "timeout. Read-only commands and GET/wget/curl reads run "
                    "without approval; install commands and unsafe paths or "
                    "non-GET downloads require approval and are remembered after "
                    "approval. Approving one curl command trusts all later curl "
                    "variations. Never put passwords or API tokens inline; store "
                    "credentials in the vault and pass them via a safe mechanism."
                ),
                parameters=_obj(
                    {
                        "command": _str(
                            "Shell command to run, for example `git status` or "
                            "`python script.py`."
                        ),
                        "cwd": _str(
                            "Optional working directory relative to the shell "
                            "workspace. Absolute paths outside the workspace are "
                            "rejected."
                        ),
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "Optional timeout, 1-120 seconds.",
                        },
                    },
                    ["command"],
                ),
                handler=self.run_shell,
                side_effect=True,
                always_approve=False,
            ),
        }




def _redact(value: Any, depth: int = 0) -> Any:
    """Strip likely secrets before anything reaches the audit log."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(
                marker in lowered
                for marker in ("password", "secret", "token", "api_key", "credential")
            ):
                clean[str(key)] = "[redacted]"
            else:
                clean[str(key)] = _redact(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in list(value)[:20]]
    if isinstance(value, str):
        return value[:500]
    return value


def _redact_shell_command(command: str) -> str:
    """Best-effort scrubbing before a shell command reaches the audit log."""
    text = str(command or "")
    text = re.sub(
        r"(?i)((?:authorization|token|password|secret|api[_-]?key)\s*[:=]\s*)(\S+)",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(https://)[^/\s@]+@", r"\1[redacted]@", text)
    return text[:12_000]


def _audit_args(tool: str, args: Dict[str, Any]) -> Any:
    """Redacted copy of tool arguments for the append-only audit log."""
    clean = _redact(args)
    if tool == "browser_type" and isinstance(clean, dict) and "text" in clean:
        clean["text"] = "[redacted %d chars]" % len(str(args.get("text", "")))
    if tool == "run_shell" and isinstance(clean, dict) and "command" in clean:
        clean["command"] = _redact_shell_command(str(args.get("command", "")))
    return clean


def summarise(tool: str, args: Dict[str, Any]) -> str:
    if tool == "browser_open":
        return "Open %s" % args.get("url", "")
    if tool == "browser_click":
        return "Click %r" % str(args.get("target", ""))[:120]
    if tool == "browser_type":
        return "Type into %s" % str(args.get("selector", ""))[:120]
    if tool == "browser_press":
        return "Press %s" % args.get("key", "")
    if tool == "run_shell":
        return "Run shell: %s" % str(args.get("command", ""))[:300]
    if tool == "generate_image":
        return "Generate image: %s" % str(args.get("prompt", ""))[:140]
    if tool == "schedule_task":
        return "Schedule %r (%s: %s)" % (
            str(args.get("title", ""))[:80],
            args.get("kind", ""),
            args.get("schedule", ""),
        )
    if tool == "create_agent":
        return "Create agent: %s" % str(args.get("brief", ""))[:160]
    if tool == "rename_self":
        bits = []
        if args.get("name"):
            bits.append("name %r" % str(args["name"])[:60])
        if args.get("role"):
            bits.append("role %r" % str(args["role"])[:120])
        return "Rename self to %s" % (", ".join(bits) or "(no change)")
    if tool == "assign_task":
        return "Assign %r to %s" % (
            str(args.get("title", ""))[:80],
            args.get("agent", ""),
        )
    if tool == "send_message":
        return "Message %s" % args.get("agent", "")
    if tool == "cancel_task":
        return "Delete scheduled task %s" % args.get("task_id", "")
    if tool == "vault_fill":
        label = str(args.get("label") or "").strip()
        return "Autofill vault entry %r on the current page" % (
            label or "(host match)"
        )
    if tool == "request_vault_entry":
        return "Request a vault entry for %s" % (
            str(args.get("domain") or "the requested site")[:160]
        )
    return "%s %s" % (tool, json.dumps(_redact(args), ensure_ascii=False)[:200])


async def execute_tool(
    services: Services,
    name: str,
    args: Dict[str, Any],
    chat_id: str = "default",
    actor: str = "agent",
    approved: bool = False,
) -> Dict[str, Any]:
    spec = services.tools.get(name)
    if spec is None:
        return {"error": "Unknown tool %r." % name}
    if not isinstance(args, dict):
        args = {}

    if spec.side_effect and not bool(services.prefs.get("write_enabled")):
        services.audit.log(
            actor, "tool_denied", {"tool": name, "reason": "write_disabled"}, "denied"
        )
        return {
            "error": (
                "Write access is disabled in Sentinel settings. Do not retry; "
                "ask the user to enable write access."
            )
        }

    auto = bool(services.prefs.get("auto_approve_writes"))
    auto_browser = bool(services.prefs.get("auto_approve_browser"))
    if name == "run_shell" and not bool(services.prefs.get("shell_enabled")):
        return {
            "error": (
                "Shell access is disabled. Enable it in Settings; normal "
                "workspace commands then run directly, while installs and "
                "unsafe downloads need approval."
            )
        }
    needs_approval = spec.always_approve or (spec.side_effect and not auto)
    if name in BROWSER_ROUTINE_TOOLS and auto_browser:
        needs_approval = False
    if name == "run_shell":
        command = str(args.get("command") or "")
        trusted = services.prefs.get("trusted_shell_commands") or []
        families = list(services.prefs.get("trusted_shell_families") or [])
        if not services.prefs.get("trusted_shell_families_initialized"):
            for item in trusted:
                family = shell_command_family(item)
                if family and family not in families:
                    families.append(family)
            services.prefs.update(
                {
                    "trusted_shell_families": families,
                    "trusted_shell_families_initialized": True,
                }
            )
        signature = shell_command_signature(command)
        legacy = " ".join(command.split())
        trusted_hit = signature in trusted or legacy in trusted
        reason = shell_approval_reason(command)
        family = shell_command_family(command)
        family_hit = bool(family and family in families and reason == "download")
        needs_approval = bool(reason) and not trusted_hit and not family_hit
    host = _current_host(services, chat_id) if name in BROWSER_WRITE_TOOLS else ""
    if (
        needs_approval
        and not approved
        and not spec.always_approve
        and host
        and services.trust.is_trusted(host)
    ):
        services.audit.log(
            actor, "tool_trusted_host", {"tool": name, "host": host}, "ok"
        )
        needs_approval = False
    if needs_approval and not approved:
        summary = summarise(name, args)
        approval = services.approvals.create(
            chat_id, name, args, summary, host=host
        )
        return {
            "status": "pending_approval",
            "approval_id": approval["id"],
            "tool": name,
            "host": host,
            "summary": summary,
            "message": (
                "Approval requested: %s%s. The user must approve it in the "
                "Approvals inbox. Do not retry."
                % (summary, (" on %s" % host) if host else "")
            ),
        }

    try:
        result = await asyncio.wait_for(
            spec.handler(args, chat_id), timeout=TOOL_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        services.audit.log(
            actor, "tool_%s" % name, {"args": _audit_args(name, args), "error": "timeout"}, "error"
        )
        return {"error": "Tool %s timed out after %ss." % (name, TOOL_TIMEOUT_SECONDS)}
    except Exception as exc:
        services.audit.log(
            actor,
            "tool_%s" % name,
            {"args": _audit_args(name, args), "error": "%s: %s" % (type(exc).__name__, exc)},
            "error",
        )
        return {"error": "%s: %s" % (type(exc).__name__, exc)}

    services.audit.log(actor, "tool_%s" % name, {"args": _audit_args(name, args)}, "ok")
    return {"ok": True, "tool": name, "result": result}


async def execute_approved(
    services: Services, approval: Dict[str, Any]
) -> Dict[str, Any]:
    """Run a previously stored approval exactly as the agent proposed it."""
    spec = services.tools.get(approval["tool"])
    if spec is None:
        services.approvals.resolve(
            approval["id"], "rejected", "Tool no longer exists."
        )
        return {"error": "Tool %r no longer exists." % approval["tool"]}
    if spec.side_effect and not bool(services.prefs.get("write_enabled")):
        services.approvals.resolve(
            approval["id"], "rejected", "Write access was disabled."
        )
        return {
            "error": "Write access is disabled in Sentinel settings; nothing ran."
        }
    try:
        result = await asyncio.wait_for(
            spec.handler(approval["args"], approval["chat_id"]),
            timeout=TOOL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        message = "%s: %s" % (type(exc).__name__, exc)
        services.approvals.resolve(approval["id"], "approved", "error: " + message)
        services.audit.log(
            "user",
            "tool_%s" % approval["tool"],
            {"approval_id": approval["id"], "error": message},
            "error",
        )
        return {"error": message}
    services.approvals.resolve(
        approval["id"],
        "approved",
        json.dumps(_redact(result), ensure_ascii=False)[:4000],
    )
    services.audit.log(
        "user",
        "tool_%s" % approval["tool"],
        {"approval_id": approval["id"], "args": _audit_args(approval["tool"], approval["args"])},
        "ok",
    )
    return {"ok": True, "tool": approval["tool"], "result": result}
