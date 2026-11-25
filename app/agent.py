"""The agent loop: OpenAI tool calling, persistence and approval hand-off."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .openai_client import OpenAIConfigError
from .services import Services
from .tools import execute_tool
from .uploads import user_content

MAX_MESSAGE_CHARS = 8000
HISTORY_LIMIT = 30


class AgentError(RuntimeError):
    pass


class Agent:
    def __init__(
        self, services: Services, profile: Optional[Dict[str, Any]] = None
    ) -> None:
        self.services = services
        self.profile = profile or {}

    # -- prompt -----------------------------------------------------------
    def system_prompt(self) -> str:
        prefs = self.services.prefs.all()
        tz_name = prefs.get("timezone") or self.services.settings.scheduler_timezone
        try:
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            tz_name = "UTC"
            now = datetime.now(ZoneInfo("UTC"))
        rows = self.services.db.query(
            "SELECT content FROM memories ORDER BY id DESC LIMIT 12"
        )
        facts = "\n".join("- " + row["content"] for row in rows) or "- (nothing yet)"
        write_state = "enabled" if prefs.get("write_enabled") else "DISABLED"
        auto_state = "on" if prefs.get("auto_approve_writes") else "off"
        browser_auto_state = (
            "on" if prefs.get("auto_approve_browser") else "off"
        )
        shell_state = "on (approval required)" if prefs.get("shell_enabled") else "off"
        profile = self.profile
        name = profile.get("name") or prefs.get("agent_name") or "Muse"
        style = prefs.get("agent_style") or "concise and helpful"

        if profile:
            identity = (
                "You are %s, %s on the user's team. You do real work rather "
                "than only answering questions." % (name, profile.get("role") or "a teammate")
            )
            team_block = (
                "\nYour standing role:\n%s\n\n"
                "Team protocol:\n"
                "- You are one named teammate. Work the assigned task end to end.\n"
                "- Use list_team to see the other specialists and their roles.\n"
                "- Use assign_task to hand a real sub-task to the best agent; "
                "include a clear title and a complete prompt.\n"
                "- Use send_message to coordinate and read_inbox to check replies.\n"
                "- Never assign the same task back to yourself.\n"
                "- Use list_team_tasks to see the shared board before you delegate.\n"
                "- If the user asks you to rename yourself or change your one-line "
                "role, use rename_self. It changes only your own label/role, not "
                "your standing instructions or existing tasks.\n"
                "- Model settings belong to the team lead; you cannot change your "
                "own or another agent's model.\n\n"
                % (profile.get("instructions") or profile.get("role") or "")
            )
        else:
            identity = (
                "You are %s, the user's personal AI agent and team lead. You do "
                "real work rather than only answering questions." % name
            )
            team_block = (
                "\nTeam tools: list_team, create_agent, set_agent_model, "
                "assign_task, send_message, read_inbox, list_team_tasks. When a "
                "goal needs more than one specialist, create or choose named "
                "agents and delegate through the shared task board instead of "
                "doing every role yourself. As team lead you may call "
                "set_agent_model to pin or clear any agent's model (for example "
                "moving a simple bot to a cheaper model).\n\n"
            )

        details = (
            "Current date and time: %s (%s).\n"
            "Sentinel: write access is %s; auto-approve writes is %s; "
            "routine browser actions are %s; shell access is %s.\n\n"
            "How you work:\n"
            "- Break a goal into steps and use tools until it is actually done.\n"
            "- Open a page before reading or interacting with it, and inspect the "
            "result of every action.\n"
            "- Routine browser clicks, typing and key presses are pre-approved "
            "when that setting is on. Do not ask the user to confirm every small "
            "browser step; proceed and report the result. Still ask before "
            "purchases, sending messages, or irreversible actions.\n"
            "- Use remember/recall for durable preferences. Never store secrets.\n"
            "- Timestamps: never parrot a raw time. Call convert_time (or convert "
            "it yourself) and always state both ET (America/New_York) and JST "
            "(Asia/Tokyo), e.g. '06:30 JST = 16:30 ET the previous day'.\n"
            "- Data hygiene: before trusting a data file, check its mtime "
            "(file_info or ls -la). A stale local copy is not evidence that a "
            "pipeline is down; verify the live path or endpoint first.\n"
            "- Use schedule_task for anything that should happen later or "
            "repeatedly. Explain the schedule you created. Scheduled runs do not "
            "share your chat memory, so put every needed detail (paths, labels, "
            "IDs, goals) in the task prompt itself.\n"
            "- Use generate_text for focused long-form writing and generate_image "
            "when the user wants a picture (it costs money).\n"
            "- Use run_shell for git, curl, Python scripts, and other command-line "
            "work when the browser UI is not the right tool. Shell commands run "
            "in the workspace. Normal workspace commands and GET/wget/curl reads "
            "run directly; install commands, unsafe paths, and non-GET downloads "
            "need approval and are remembered after approval. Approving one curl "
            "command trusts all later curl variations. Never put passwords or "
            "API tokens inline, and never use shell to bypass approvals or "
            "read/exfiltrate secrets.\n"
            "- Work efficiently. Do not repeat the same failed action; prefer "
            "direct URLs, sitemaps and already-open pages over search engines, "
            "and batch independent reads in one turn.\n"
            "- Do not talk about internal token, step, time or money budgets. "
            "If you must stop, state the concrete blocker and the next step.\n\n"
            "Safety rules (non-negotiable):\n"
            "- Never ask the user to paste a password into chat, and never repeat "
            "a secret. Credentials live in the vault and are only used through "
            "vault_fill, which autofills a domain-bound entry after approval.\n"
            "- If a login is needed and vault_list has no matching entry, open the "
            "login page and call request_credential. Tell the user a credential "
            "card is waiting; never ask for the password in chat. The system saves "
            "it, submits the login and continues automatically.\n"
            "- If a login or browser action lands on a one-time-code, OTP, 2FA or "
            "verification screen, prefer request_otp. If the user sends a code "
            "in chat, accept it and use it; the system may submit it before you "
            "see this message. Never reject or scold the user for sending a code, "
            "and never repeat the code in a reply, note or summary. vault_fill "
            "may return status=otp_required; in that case do not retry, tell the "
            "user the card is waiting.\n"
            "- If the vault is locked, or no entry exists and the user wants to "
            "supply access, call request_vault_entry with the domain. A secure "
            "vault card appears; the user can unlock the vault and add the entry "
            "there. Do not stop at a refusal or ask for the password in chat.\n"
            "- request_vault_entry only saves the entry; it does not attempt a "
            "login. Use request_credential when an actual login form is open.\n"
            "- If vault_list shows an entry bound to the current host, call "
            "vault_fill with no label; Muse selects that entry by hostname.\n"
            "- If the vault is locked, tell the user to unlock it in the Vault "
            "tab; do not try to work around it.\n"
            "- Some tool calls return status=pending_approval. When that happens, "
            "tell the user the approval id and what it will do, then stop. Never "
            "retry a pending action.\n"
            "- Respect read-only mode. If a tool reports that write access is "
            "disabled, ask the user to enable it in Settings.\n"
            "- Do not invent tool results. If something failed, say so plainly.\n"
            "- Before a purchase, sending a message, or anything irreversible, "
            "state exactly what will happen and wait for approval.\n\n"
            "Style: %s. Keep replies short, plain text, and skimmable. When a "
            "scheduled task fires, execute it and summarise the outcome without "
            "asking follow-up questions.\n\n"
            "Known facts about the user:\n%s\n"
            % (
                now.strftime("%A, %d %B %Y, %H:%M"),
                tz_name,
                write_state,
                auto_state,
                browser_auto_state,
                shell_state,
                style,
                facts,
            )
        )
        return identity + "\n\n" + details + team_block

    # -- persistence ------------------------------------------------------
    def _store(
        self,
        chat_id: str,
        role: str,
        content: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.services.db.execute(
            "INSERT INTO messages (chat_id, role, content, attachments_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                chat_id,
                role,
                content or "",
                json.dumps(attachments or [], ensure_ascii=False),
                time.time(),
            ),
        )

    def record_user(
        self,
        chat_id: str,
        text: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist a user message that arrived while a run was active."""
        self._store(chat_id, "user", text, attachments or [])

    def _drain_queued(
        self, chat_id: str, messages: List[Dict[str, Any]]
    ) -> List[Any]:
        queue = getattr(self.services, "chat_queue", None)
        if queue is None:
            return []
        queued = queue.drain(chat_id)
        for item in queued:
            if isinstance(item, dict):
                content = user_content(
                    str(item.get("text") or ""),
                    item.get("attachments") or [],
                    self.services.settings.uploads_dir,
                )
            else:
                content = str(item)
            messages.append({"role": "user", "content": content})
        return queued

    def note(self, chat_id: str, text: str) -> None:
        self._store(chat_id, "tool_note", text)

    def attach(
        self,
        chat_id: str,
        text: str,
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._store(chat_id, "assistant", text, images or [])

    def history(
        self, chat_id: str, limit: int = HISTORY_LIMIT
    ) -> List[Dict[str, Any]]:
        rows = self.services.db.query(
            "SELECT role, content, attachments_json FROM messages WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        out: List[Dict[str, Any]] = []
        for row in reversed(rows):
            role = row["role"]
            content = (row["content"] or "").strip()
            attachments: List[Dict[str, Any]] = []
            try:
                parsed = json.loads(row["attachments_json"] or "[]")
                if isinstance(parsed, list):
                    attachments = [item for item in parsed if isinstance(item, dict)]
            except (TypeError, ValueError):
                attachments = []
            if role == "tool_note":
                if content:
                    out.append({"role": "system", "content": content[:4000]})
            elif role == "user" and attachments:
                out.append(
                    {
                        "role": "user",
                        "content": user_content(
                            content,
                            attachments,
                            self.services.settings.uploads_dir,
                        ),
                    }
                )
            elif role in ("user", "assistant") and content:
                out.append({"role": role, "content": content[:6000]})
        return out

    def history_for_ui(self, chat_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.services.db.query(
            "SELECT id, role, content, attachments_json, created_at FROM messages "
            "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, max(1, min(int(limit), 300))),
        )
        for row in rows:
            try:
                row["attachments"] = json.loads(row.pop("attachments_json") or "[]")
            except ValueError:
                row["attachments"] = []
        return list(reversed(rows))

    def clear(self, chat_id: str) -> None:
        self.services.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        self.services.audit.log("user", "chat_clear", chat_id)

    @staticmethod
    def _step_summary(step: Dict[str, Any]) -> str:
        name = step.get("tool") or "tool"
        if step.get("ok"):
            return "%s ok" % name
        if step.get("status") == "pending_approval":
            return "%s waiting for approval" % name
        if step.get("status") == "credential_required":
            return "%s waiting for a credential" % name
        error = (step.get("error") or "failed").replace("\n", " ")
        return "%s failed: %s" % (name, error[:120])

    def _waiting_reply(
        self,
        pending: List[Dict[str, Any]],
        pending_credentials: List[Dict[str, Any]],
    ) -> str:
        parts: List[str] = []
        for item in pending:
            summary = item.get("summary") or item.get("tool") or "an action"
            parts.append(
                "Approval #%s is waiting: %s. Approve it in Approvals; Muse "
                "will continue automatically." % (item.get("approval_id"), summary)
            )
        for item in pending_credentials:
            domain = item.get("domain") or "this site"
            parts.append(
                "A credential card is waiting for %s. Submit it there; Muse "
                "will sign in and continue automatically." % domain
            )
        return " ".join(parts) or "I need your input before continuing."

    def _stopped_reply(
        self,
        max_steps: int,
        steps: List[Dict[str, Any]],
        pending: List[Dict[str, Any]],
        pending_credentials: List[Dict[str, Any]],
    ) -> str:
        head = (
            "I used all %d tool rounds for this message before finishing."
            % max_steps
        )
        recent = [self._step_summary(step) for step in steps[-3:]]
        detail = (" Last actions: %s." % "; ".join(recent)) if recent else ""
        if pending or pending_credentials:
            return head + detail + " " + self._waiting_reply(
                pending, pending_credentials
            )
        return (
            head
            + detail
            + " Reply 'continue' and I'll resume from the current page with a "
            "fresh attempt."
        )

    # -- loop -------------------------------------------------------------
    async def run(
        self,
        chat_id: str = "default",
        user_text: str = "",
        actor: str = "agent",
        attachments: Optional[List[Dict[str, Any]]] = None,
        store_user: bool = True,
    ) -> Dict[str, Any]:
        text = (user_text or "").strip()
        attachments = attachments or []
        if not text and not attachments:
            raise AgentError("Message is empty.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise AgentError(
                "Message is too long (max %d characters)." % MAX_MESSAGE_CHARS
            )
        if not self.services.openai.configured:
            raise AgentError(
                "OPENAI_API_KEY is not configured. Add it to .env and restart Muse."
            )

        if store_user:
            self._store(chat_id, "user", text, attachments)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()}
        ]
        messages.extend(self.history(chat_id))
        if not store_user:
            messages.append(
                {
                    "role": "user",
                    "content": user_content(
                        text, attachments, self.services.settings.uploads_dir
                    ),
                }
            )
        schemas = [spec.schema() for spec in self.services.tools.values()]
        # Named agents may pin their own model; the provider default is used
        # for Muse itself and for agents without an override.
        model = ((self.profile or {}).get("model") or "").strip() or None
        cancel = None
        if getattr(self.services, "cancellations", None) is not None:
            cancel = self.services.cancellations.begin(chat_id)

        steps: List[Dict[str, Any]] = []
        images: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        pending_credentials: List[Dict[str, Any]] = []
        final_reply = ""
        max_steps = int(self.services.settings.max_agent_steps)
        limited = max_steps > 0
        tool_counts: Dict[str, int] = {}
        stalled_tool: Optional[str] = None
        stall_count = 0
        nudged_at = 0
        round_index = 0

        while True:
            if cancel is not None and cancel.is_set():
                final_reply = (
                    "Stopped at your request. The tool steps above show what ran."
                )
                break
            self._drain_queued(chat_id, messages)
            if limited and round_index >= max_steps:
                final_reply = self._stopped_reply(
                    max_steps, steps, pending, pending_credentials
                )
                break
            if limited:
                remaining = max_steps - round_index
                if (
                    round_index > 0
                    and 0 < remaining <= 2
                    and remaining != nudged_at
                ):
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Step limit: only %d model round(s) remain. "
                                "Stop calling tools and write your final answer "
                                "now using what you have. If the task is "
                                "incomplete, state exactly what remains and what "
                                "the user should tell you to continue." % remaining
                            ),
                        }
                    )
                    nudged_at = remaining
            try:
                response = await self.services.openai.achat(
                    messages, tools=schemas, session_id=chat_id, model=model
                )
            except OpenAIConfigError as exc:
                raise AgentError(str(exc))
            except Exception as exc:
                raise AgentError("OpenAI request failed: %s" % exc)

            try:
                message = response.choices[0].message
            except (AttributeError, IndexError):
                raise AgentError("The model returned an unexpected response.")

            tool_calls = list(getattr(message, "tool_calls", None) or [])
            content = (getattr(message, "content", None) or "").strip()

            if not tool_calls:
                if self._drain_queued(chat_id, messages):
                    continue
                final_reply = content or "Done."
                break

            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments or "{}",
                        },
                    }
                    for call in tool_calls
                ],
            }
            messages.append(assistant_message)
            if content:
                self._store(chat_id, "assistant", content)

            for call in tool_calls:
                if cancel is not None and cancel.is_set():
                    final_reply = (
                        "Stopped at your request. The tool steps above show what ran."
                    )
                    break
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    args = {}
                    result: Dict[str, Any] = {
                        "error": "The tool arguments were not valid JSON."
                    }
                else:
                    signature = "%s:%s" % (
                        name,
                        json.dumps(args, sort_keys=True, ensure_ascii=False),
                    )
                    tool_counts[signature] = tool_counts.get(signature, 0) + 1
                    if tool_counts[signature] > 3:
                        result = {
                            "ok": False,
                            "error": (
                                "This exact tool call has already been tried "
                                "three times. Do not repeat it; change approach "
                                "or answer with what you have."
                            ),
                        }
                        stall_count += 1
                        if stall_count >= 3:
                            stalled_tool = name
                    else:
                        try:
                            result = await execute_tool(
                                self.services,
                                name,
                                args,
                                chat_id=chat_id,
                                actor=actor,
                            )
                        except Exception as exc:  # never break the loop
                            result = {"error": "Tool dispatch failed: %s" % exc}

                step: Dict[str, Any] = {
                    "tool": name,
                    "args": args,
                    "ok": bool(result.get("ok")),
                    "status": result.get("status"),
                    "error": result.get("error"),
                    "approval_id": result.get("approval_id"),
                }
                inner = result.get("result")
                if isinstance(inner, dict):
                    step["result"] = inner
                    if inner.get("image_url"):
                        images.append(
                            {
                                "image_url": inner["image_url"],
                                "artifact_id": inner.get("artifact_id"),
                            }
                        )
                steps.append(step)
                if result.get("status") == "pending_approval":
                    pending.append(
                        {
                            "approval_id": result.get("approval_id"),
                            "tool": name,
                            "summary": result.get("summary"),
                            "host": result.get("host"),
                        }
                    )
                if result.get("status") == "credential_required":
                    pending_credentials.append(
                        {
                            "request_id": result.get("request_id"),
                            "domain": result.get("domain"),
                            "label": result.get("label"),
                            "login_url": result.get("login_url"),
                            "message": result.get("message"),
                        }
                    )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False)[:20000],
                    }
                )
            if cancel is not None and cancel.is_set():
                if not final_reply:
                    final_reply = (
                        "Stopped at your request. The tool steps above show what ran."
                    )
                break
            if stalled_tool:
                final_reply = (
                    content
                    or "I couldn't complete that step with the current page. "
                    "Try a different page, selector or approach."
                )
                break
            if pending or pending_credentials:
                final_reply = self._waiting_reply(pending, pending_credentials)
                break
            round_index += 1

        if not final_reply:
            final_reply = "I could not produce a reply. Please try again."
        if cancel is not None:
            self.services.cancellations.finish(chat_id, cancel)
        unique_images = list({img["image_url"]: img for img in images}.values())
        self._store(chat_id, "assistant", final_reply, unique_images)
        return {
            "chat_id": chat_id,
            "reply": final_reply,
            "steps": steps,
            "images": unique_images,
            "pending_approvals": pending,
            "pending_credentials": pending_credentials,
        }
