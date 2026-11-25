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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .services import Services
from .shell import (
    ShellError,
    run_shell_command,
    shell_approval_reason,
    shell_command_family,
    shell_command_signature,
    workspace_path,
)
from .team import TeamError, parse_chat_context
from .vault import EntryNotFound, VaultLocked, host_matches

TOOL_TIMEOUT_SECONDS = 180
BROWSER_WRITE_TOOLS = {
    "browser_click",
    "browser_type",
    "browser_press",
    "vault_fill",
}
BROWSER_ROUTINE_TOOLS = {"browser_click", "browser_type", "browser_press"}

SECRET_PLACEHOLDER_RE = re.compile(r"\{\{\s*secret:\s*([^{}]+?)\s*\}\}")
SECRET_FIELDS = ("password", "username", "notes", "url", "totp_secret")
MIN_REDACT_CHARS = 4


def _resolve_shell_secrets(
    services: Services, command: str
) -> Tuple[str, Dict[str, str], List[str]]:
    """Swap {{secret:Label[.field]}} placeholders for injected env references.

    Returns the rewritten command (no secrets), the environment additions and
    the resolved values (for output redaction). Raises VaultLocked /
    EntryNotFound / ValueError so the caller can explain what is missing.
    """
    env: Dict[str, str] = {}
    values: List[str] = []
    counter = {"next": 0}

    def replace(match: "re.Match[str]") -> str:
        raw = match.group(1).strip()
        label = raw
        field = "password"
        if "." in raw:
            candidate_label, _, candidate_field = raw.rpartition(".")
            if candidate_field.strip().lower() in SECRET_FIELDS:
                label = candidate_label.strip()
                field = candidate_field.strip().lower()
        secret = services.vault.get_secret_by_label(label)
        value = str(secret.get(field) or "")
        if not value:
            raise ValueError("Vault entry %r has no %s value." % (label, field))
        counter["next"] += 1
        key = "MUSE_SECRET_%d" % counter["next"]
        env[key] = value
        values.append(value)
        return "$" + key

    resolved = SECRET_PLACEHOLDER_RE.sub(replace, command)
    return resolved, env, values


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

    # -- utilities --------------------------------------------------------
    async def convert_time(self, args: Dict[str, Any], chat_id: str) -> Any:
        raw = str(args.get("timestamp") or "").strip()
        source_tz = str(args.get("from_timezone") or "").strip() or str(
            self.services.prefs.get("timezone")
            or self.services.settings.scheduler_timezone
            or "UTC"
        )
        moment = _parse_moment(raw, source_tz)
        et = moment.astimezone(ZoneInfo("America/New_York"))
        jst = moment.astimezone(ZoneInfo("Asia/Tokyo"))
        return {
            "epoch": moment.timestamp(),
            "utc": moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "et": et.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "jst": jst.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "source_timezone": source_tz,
            "note": "When reporting times, always state both ET and JST.",
        }

    async def file_info(self, args: Dict[str, Any], chat_id: str) -> Any:
        raw = _require_text(args, "path", 500)
        base = workspace_path(self.services.settings)
        target = Path(raw)
        if not target.is_absolute():
            target = base / target
        try:
            target = target.resolve()
            target.relative_to(base)
        except (OSError, ValueError):
            raise ValueError(
                "file_info only inspects files inside the shell workspace (%s). "
                "Use run_shell `ls -la` for other paths." % base
            )
        if not target.exists():
            return {"path": str(target), "exists": False}
        stat = target.stat()
        age = max(0.0, time.time() - stat.st_mtime)
        result: Dict[str, Any] = {
            "path": str(target),
            "exists": True,
            "is_dir": target.is_dir(),
            "size": stat.st_size,
            "mtime_epoch": stat.st_mtime,
            "mtime_utc": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(timespec="seconds"),
            "age_seconds": int(age),
            "age_human": _human_age(age),
        }
        if target.is_file() and age > 7 * 86400:
            result["stale_warning"] = (
                "This file is over a week old. Do not conclude that a pipeline "
                "or service is down from this copy; confirm the live path or "
                "endpoint first."
            )
        return result

    # -- scheduling -------------------------------------------------------
    def _schedule_context(self, chat_id: str) -> str:
        """Snapshot recent chat turns so the scheduled run carries its context."""
        try:
            history = self.services.agent.history(chat_id, limit=8)
        except Exception:
            return ""
        lines: List[str] = []
        for message in history[-6:]:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "").strip()
            if not content or role == "tool":
                continue
            lines.append("%s: %s" % (role, content[:300]))
        return "\n".join(lines)[-2500:]

    async def schedule_task(self, args: Dict[str, Any], chat_id: str) -> Any:
        title = _require_text(args, "title", 120)
        prompt = _require_text(args, "prompt")
        kind = str(args.get("kind", "")).strip()
        schedule = str(args.get("schedule", "")).strip()
        timezone = str(args.get("timezone") or "").strip() or None
        agent_id = None
        agent_name = ""
        raw_agent = args.get("agent_id") or args.get("agent")
        if raw_agent:
            try:
                agent = self._resolve_team_agent({"agent": raw_agent})
            except (ValueError, TeamError) as exc:
                available = ", ".join(
                    "%s (#%s)" % (item.get("name"), item.get("id"))
                    for item in self.services.team.list_agents()
                )
                raise ValueError(
                    "Unknown agent %r. Active agents: %s"
                    % (raw_agent, available or "(none)")
                ) from exc
            if agent.get("status") != "active":
                raise ValueError(
                    "Agent %s is not active." % (agent.get("name") or raw_agent)
                )
            agent_id = int(agent["id"])
            agent_name = agent.get("name") or ""
        task = self.services.scheduler.create(
            title,
            prompt,
            kind,
            schedule,
            timezone,
            agent_id=agent_id,
            context=self._schedule_context(chat_id),
        )
        return {
            "task_id": task["id"],
            "title": task["title"],
            "kind": task["kind"],
            "next_run_at": task["next_run_at"],
            "agent": agent_name,
            "message": (
                ("Runs as %s and its pinned model." % agent_name)
                if agent_name
                else "Runs as Muse with the provider's default model."
            ),
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
                    "agent": task.get("agent_name") or "",
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
        extra_env: Dict[str, str] = {}
        secret_values: List[str] = []
        if SECRET_PLACEHOLDER_RE.search(command):
            try:
                command, extra_env, secret_values = _resolve_shell_secrets(
                    self.services, command
                )
            except VaultLocked:
                return {
                    "error": (
                        "The vault is locked. Unlock it in the Vault tab, then "
                        "retry; {{secret:Label}} placeholders cannot be resolved "
                        "while it is locked."
                    )
                }
            except EntryNotFound as exc:
                try:
                    labels = ", ".join(
                        entry["label"] for entry in self.services.vault.list_entries()
                    ) or "(none yet)"
                except Exception:
                    labels = "(unavailable)"
                return {"error": "%s Available labels: %s" % (exc, labels)}
            except ValueError as exc:
                return {"error": str(exc)}
        try:
            result = await run_shell_command(
                self.services.settings,
                command,
                cwd=cwd,
                timeout_seconds=timeout,
                extra_env=extra_env or None,
            )
        except ShellError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
        if secret_values:
            # Best effort: the model must never read a resolved secret back.
            for field_name in ("stdout", "stderr", "command"):
                text = str(result.get(field_name) or "")
                for secret in secret_values:
                    if secret and len(secret) >= MIN_REDACT_CHARS:
                        text = text.replace(secret, "[redacted]")
                result[field_name] = text
            result["secrets_used"] = len(secret_values)
        return result

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
        kind = str(args.get("kind") or "auto").strip().lower()[:20]
        return await self.services.browser.click(
            target, session=_browser_session(chat_id), kind=kind
        )

    async def browser_type(self, args: Dict[str, Any], chat_id: str) -> Any:
        selector = _require_text(args, "selector", 500)
        text = str(args.get("text", ""))[:4000]
        submit = bool(args.get("submit", False))
        kind = str(args.get("kind") or "auto").strip().lower()[:20]
        return await self.services.browser.type_text(
            selector, text, submit, session=_browser_session(chat_id), kind=kind
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
        try:
            return self.services.team.get_agent(name=name)
        except TeamError:
            lead = self.services.team.lead_profile()
            aliases = {
                lead["name"].lower(),
                "muse",
                "lead",
                "team lead",
                "the lead",
                "owner",
            }
            if name.lower() in aliases:
                return lead
            raise

    async def list_team(self, args: Dict[str, Any], chat_id: str) -> Any:
        me = self._team_agent(chat_id)
        lead = self.services.team.lead_profile()
        return {
            "me": me,
            "lead": {
                "id": lead["id"],
                "name": lead["name"],
                "role": lead["role"],
                "note": (
                    "The team lead is not a task assignee; send_message to the "
                    "lead to report results or ask a question."
                ),
            },
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

    async def set_agent_model(self, args: Dict[str, Any], chat_id: str) -> Any:
        """Team lead only: pin (or clear) the model of any named agent."""
        me, _task = parse_chat_context(chat_id)
        if me:
            raise ValueError(
                "Only the team lead can change another agent's model. Ask the "
                "lead to do it."
            )
        agent = self._resolve_team_agent(args)
        if int(agent.get("id") or 0) == 0:
            raise ValueError(
                "That is the team lead itself; its model comes from the Provider "
                "tab, not from an agent profile."
            )
        model = str(args.get("model") or "").strip()[:160]
        previous = str(agent.get("model") or "")
        updated = self.services.team.update_agent(
            int(agent["id"]), {"model": model}, actor="lead"
        )
        current = updated.get("model") or ""
        return {
            "agent_id": updated["id"],
            "name": updated["name"],
            "previous_model": previous,
            "model": current,
            "message": (
                "%s now runs on %s."
                % (
                    updated["name"],
                    current or "the provider's default model",
                )
            ),
        }

    async def create_agent(self, args: Dict[str, Any], chat_id: str) -> Any:
        brief = _require_text(args, "brief")
        name = str(args.get("name") or "").strip()[:60] or None
        role = str(args.get("role") or "").strip()[:200] or None
        model = str(args.get("model") or "").strip()[:160] or None
        me = self._team_agent(chat_id)
        created_by = "agent:%d" % me if me else "user"
        agent = await self.services.team.create_from_prompt(
            brief, name=name, role=role, created_by=created_by, model=model
        )
        return {
            "agent_id": agent["id"],
            "name": agent["name"],
            "role": agent["role"],
            "status": agent["status"],
            "model": agent.get("model") or "",
            "message": (
                "Created %s. It runs on %s."
                % (
                    agent["name"],
                    agent.get("model") or "the provider's default model",
                )
            ),
        }

    async def assign_task(self, args: Dict[str, Any], chat_id: str) -> Any:
        agent = self._resolve_team_agent(args)
        if int(agent.get("id") or 0) == 0:
            raise ValueError(
                "The team lead (%s) is not a task assignee; use send_message to "
                "report to them instead." % agent.get("name")
            )
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
        target = int(agent.get("id") or 0)
        message = self.services.team.send_message(
            from_agent_id=me,
            to_agent_id=target or None,  # 0 = the team lead -> Muse inbox
            content=content,
            task_id=task_id,
            kind="message",
            actor="agent:%d" % me if me else "user",
        )
        return {
            "message_id": message["id"],
            "to": message["to_name"] or agent.get("name"),
            "task_id": message["task_id"],
        }

    async def read_inbox(self, args: Dict[str, Any], chat_id: str) -> Any:
        me = self._team_agent(chat_id)
        if not me:
            raw = args.get("agent_id") or args.get("agent")
            if raw:
                me = int(self._resolve_team_agent({"agent": raw})["id"])
        rows = self.services.team.inbox(
            me,
            limit=_clamp_int(args.get("limit"), 10, 1, 50),
            unread_only=bool(args.get("unread_only", False)),
            mark_read=bool(args.get("mark_read", True)),
        )
        return {
            "agent_id": me,
            "inbox": "agent" if me else "muse",
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
                    "error": (row.get("error") or "")[:1000],
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
        specs.update(self._utility_specs())
        return specs

    def _utility_specs(self) -> Dict[str, ToolSpec]:
        return {
            "convert_time": ToolSpec(
                name="convert_time",
                description=(
                    "Convert a timestamp to ET and JST (plus UTC/epoch). Use this "
                    "before quoting any time, and always report both ET and JST."
                ),
                parameters=_obj(
                    {
                        "timestamp": _str("ISO timestamp or epoch seconds."),
                        "from_timezone": _str(
                            "IANA zone to assume when the timestamp has no offset."
                        ),
                    },
                    ["timestamp"],
                ),
                handler=self.convert_time,
            ),
            "file_info": ToolSpec(
                name="file_info",
                description=(
                    "Stat a file inside the shell workspace: size, mtime in UTC, "
                    "human age, and a stale-file warning. Check this before "
                    "trusting a data file."
                ),
                parameters=_obj(
                    {"path": _str("Path inside the workspace.")}, ["path"]
                ),
                handler=self.file_info,
            ),
        }

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
            "set_agent_model": ToolSpec(
                name="set_agent_model",
                description=(
                    "Team lead only. Change which model a named agent runs on "
                    "(pass the agent id/name and a model id; an empty model "
                    "restores the provider default). Use it to move simple bots "
                    "to a cheaper model or to upgrade one that needs more "
                    "capability. This is a configuration change and needs "
                    "approval."
                ),
                parameters=_obj(
                    {
                        "agent": _str("Agent id or exact name."),
                        "model": _str(
                            "Model id, e.g. hy4-flash-free. Empty string resets "
                            "to the provider default."
                        ),
                    },
                    ["agent", "model"],
                ),
                handler=self.set_agent_model,
                side_effect=True,
            ),
            "create_agent": ToolSpec(
                name="create_agent",
                description=(
                    "Create a new named specialist from a plain-language brief. "
                    "Muse designs the name, role and standing instructions. The "
                    "new agent can receive tasks. Optionally pin a model id "
                    "(e.g. a cheaper model) for this agent; omit it to use the "
                    "provider default. Creation is audited and capped by "
                    "TEAM_MAX_AGENTS."
                ),
                parameters=_obj(
                    {
                        "brief": _str(
                            "What this agent owns, what it should produce, and "
                            "what it should not do."
                        ),
                        "name": _str("Optional exact name."),
                        "role": _str("Optional one-line role override."),
                        "model": _str(
                            "Optional model id for this agent (use list of "
                            "models from provider if unsure). Empty = provider "
                            "default."
                        ),
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
                    "'0 8 * * 1-5'. The prompt is sent to you when the task fires. "
                    "Optionally assign the task to a named agent, which then runs "
                    "it with that agent's pinned model (useful for cheap models)."
                ),
                parameters=_obj(
                    {
                        "title": _str("Short task title."),
                        "prompt": _str("Instruction to run at fire time."),
                        "kind": _str("Schedule type.", ["once", "interval", "cron"]),
                        "schedule": _str("Datetime, duration or cron expression."),
                        "timezone": _str("Optional IANA timezone, e.g. Europe/Berlin."),
                        "agent": _str(
                            "Optional agent id or name to run this task as; "
                            "omit to run as Muse with the default model."
                        ),
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
                    "Click an element by CSS selector, ARIA role, label, "
                    "placeholder, visible text, or input value/alt/title — Muse "
                    "tries all of these across the page and its iframes and uses "
                    "the first visible match. A blocked click retries with force "
                    "and after pressing Escape, so overlays/backdrops are less "
                    "likely to stop it. This changes page state and needs "
                    "approval."
                ),
                parameters=_obj(
                    {
                        "target": _str(
                            "CSS selector, visible text, label, placeholder or "
                            "button value, e.g. 'Confirm' or '#submit'."
                        ),
                        "kind": _str(
                            "Optional match strategy: auto (default), css, text, "
                            "label, placeholder, role, value.",
                            [
                                "auto",
                                "css",
                                "text",
                                "label",
                                "placeholder",
                                "role",
                                "value",
                            ],
                        ),
                    },
                    ["target"],
                ),
                handler=self.browser_click,
                side_effect=True,
            ),
            "browser_type": ToolSpec(
                name="browser_type",
                description=(
                    "Type into an input found by CSS selector, label, "
                    "placeholder, visible text or ARIA role (searched across the "
                    "page and its iframes), optionally pressing Enter. Needs "
                    "approval."
                ),
                parameters=_obj(
                    {
                        "selector": _str(
                            "CSS selector, label, placeholder or visible text of "
                            "the input."
                        ),
                        "text": _str("Text to type."),
                        "submit": {"type": "boolean", "description": "Press Enter."},
                        "kind": _str(
                            "Optional match strategy: auto (default), css, text, "
                            "label, placeholder, role, value.",
                            [
                                "auto",
                                "css",
                                "text",
                                "label",
                                "placeholder",
                                "role",
                                "value",
                            ],
                        ),
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
                    "values are never available to you. To use a stored secret in "
                    "a shell command, pass a {{secret:Label}} placeholder to "
                    "run_shell instead of asking for the value."
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
                    "auto-detect the visible login fields. It handles both "
                    "single-page forms and two-step username/next/password forms. "
                    "You never see the username or password."
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
                    "variations. Never put passwords or API tokens inline. To use "
                    "a stored secret without seeing it, write a vault placeholder "
                    "such as {{secret:Label}} (or {{secret:Label.username}}, "
                    ".notes, .url, .totp_secret) inside the command; Muse injects "
                    "the value as a temporary environment variable at run time, "
                    "the output is redacted, and any command with a placeholder "
                    "needs approval unless the exact command is trusted."
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
        summary = "Schedule %r (%s: %s)" % (
            str(args.get("title", ""))[:80],
            args.get("kind", ""),
            args.get("schedule", ""),
        )
        agent = args.get("agent_id") or args.get("agent")
        if agent:
            summary += " as agent %s" % str(agent)[:80]
        return summary
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


def _human_age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    for unit, size in (
        ("year", 365 * 86400),
        ("month", 30 * 86400),
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
    ):
        if seconds >= size:
            value = int(seconds // size)
            return "%d %s%s ago" % (value, unit, "" if value == 1 else "s")
    return "%d seconds ago" % int(seconds)


def _parse_moment(raw: str, tz_name: str) -> datetime:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("timestamp is required (ISO string or epoch seconds).")
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except ValueError:
        pass
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            "Could not parse %r. Use an ISO timestamp (2026-11-25T16:30:00) or "
            "epoch seconds." % text
        ) from exc
    if moment.tzinfo is None:
        try:
            zone = ZoneInfo(tz_name or "UTC")
        except Exception:
            zone = timezone.utc
        moment = moment.replace(tzinfo=zone)
    return moment


def _team_agent_for_chat(services: Services, chat_id: str) -> Optional[int]:
    try:
        me, _task = parse_chat_context(chat_id)
    except Exception:
        return None
    return int(me) if me else None


def _agent_trusted_commands(services: Services, agent_id: int) -> List[str]:
    try:
        agent = services.team.get_agent(int(agent_id))
    except Exception:
        return []
    raw = agent.get("trusted_commands")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()][:50]


def _agent_command_trusted(command: str, patterns: List[str]) -> bool:
    """Match a command against an agent's trusted prefixes (trailing * = prefix)."""
    text = " ".join(str(command or "").split())
    if not text:
        return False
    for pattern in patterns:
        candidate = " ".join(str(pattern or "").split())
        if not candidate:
            continue
        if candidate.endswith("*"):
            if text.startswith(candidate[:-1]):
                return True
        elif text == candidate:
            return True
    return False


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
    if (name in BROWSER_WRITE_TOOLS or name == "browser_open") and str(
        services.prefs.get("browser_control") or "agent"
    ) == "user":
        services.audit.log(actor, "tool_denied", {"tool": name, "reason": "user_has_browser"}, "denied")
        return {
            "error": (
                "The user has taken manual control of the browser. Do not retry; "
                "wait until they release control."
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
        # Placeholders resolve to real vault secrets at run time, so any command
        # that uses one is a consequential action by default.
        has_secrets = bool(SECRET_PLACEHOLDER_RE.search(command))
        needs_approval = (
            bool(reason) or has_secrets
        ) and not trusted_hit and not family_hit
        if needs_approval and not approved and not spec.always_approve:
            me = _team_agent_for_chat(services, chat_id)
            if me is not None:
                patterns = _agent_trusted_commands(services, me)
                if _agent_command_trusted(command, patterns):
                    services.audit.log(
                        actor,
                        "tool_trusted_agent_command",
                        {"tool": name, "agent_id": me},
                        "ok",
                    )
                    needs_approval = False
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
