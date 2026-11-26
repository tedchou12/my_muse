"""HTTP API for Muse. Single user, cookie session, CSRF header on writes."""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import BUILD, __version__
from .agent import Agent, AgentError
from .approvals import ApprovalError
from .browser import BrowserError, BrowserUnavailable
from .providers import ALLOWED_PRESETS
from .credentials import CredentialRequestError
from .scheduler import ScheduleError
from .scope import LEAD_SCOPE, scope_for_agent
from .team import (
    AgentNotFound,
    DuplicateAgent,
    TaskNotFound,
    TeamError,
    TeamLimit,
    parse_chat_context,
)
from .security import (
    COOKIE_NAME,
    SlidingWindowLimiter,
    client_key,
    create_session,
    password_matches,
    request_origin_ok,
    verify_session,
)
from .services import Services
from .shell import (
    shell_command_family,
    shell_command_signature,
    shell_trusted_prefix,
)
from .tools import execute_approved
from .uploads import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILES,
    UploadError,
    attachment_from_input,
    attachment_for,
    path_for,
    save_upload,
)
from .vault import (
    DuplicateLabel,
    EntryNotFound,
    InvalidMasterPassword,
    VaultAlreadyInitialized,
    VaultError,
    VaultLocked,
    host_matches,
)

router = APIRouter()
login_limiter = SlidingWindowLimiter(max_events=5, window_seconds=60)
vault_limiter = SlidingWindowLimiter(max_events=8, window_seconds=60)


# -- dependencies ---------------------------------------------------------
def get_services(request: Request) -> Services:
    return request.app.state.services


def require_auth(
    request: Request, services: Services = Depends(get_services)
) -> str:
    token = request.cookies.get(COOKIE_NAME)
    if not verify_session(token, services.settings.session_secret):
        raise HTTPException(401, "Not authenticated.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-muse-request") != "1":
            raise HTTPException(400, "Missing X-Muse-Request header.")
        if not request_origin_ok(
            request.headers.get("origin"), request.headers.get("host")
        ):
            raise HTTPException(403, "Cross-origin request refused.")
    return "user"


def _client(request: Request) -> str:
    return client_key(request.client.host if request.client else None)


def _vault_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VaultLocked):
        return HTTPException(423, str(exc))
    if isinstance(exc, InvalidMasterPassword):
        return HTTPException(403, str(exc))
    if isinstance(exc, VaultAlreadyInitialized):
        return HTTPException(409, str(exc))
    if isinstance(exc, DuplicateLabel):
        return HTTPException(409, str(exc))
    if isinstance(exc, EntryNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, VaultError):
        return HTTPException(400, str(exc))
    return HTTPException(500, "Vault error.")


def _team_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (AgentNotFound, TaskNotFound)):
        return HTTPException(404, str(exc))
    if isinstance(exc, DuplicateAgent):
        return HTTPException(409, str(exc))
    if isinstance(exc, TeamLimit):
        return HTTPException(429, str(exc))
    if isinstance(exc, TeamError):
        return HTTPException(400, str(exc))
    return HTTPException(500, "Team error.")


def _provider_values(payload: ProviderIn) -> Dict[str, Any]:
    values = payload.model_dump()
    if values["preset"] not in ALLOWED_PRESETS:
        raise HTTPException(400, "Unknown provider preset.")
    if values["clear_api_key"] and values.get("api_key"):
        raise HTTPException(400, "Provide a new key or clear the old one, not both.")
    if values["clear_image_api_key"] and values.get("image_api_key"):
        raise HTTPException(400, "Provide a new image key or clear the old one.")

    for name in ("base_url", "image_base_url"):
        value = (values.get(name) or "").strip()
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise HTTPException(
                    400, "%s must be an http(s) URL with a host." % name
                )
            if parsed.username or parsed.password:
                raise HTTPException(
                    400, "%s must not contain embedded credentials." % name
                )
        values[name] = value

    header = (values.get("session_header") or "").strip()
    if header and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
        raise HTTPException(400, "Session header must be a valid HTTP header name.")
    values["session_header"] = header

    user_agent = (values.get("user_agent") or "").strip()
    if "\r" in user_agent or "\n" in user_agent:
        raise HTTPException(400, "User-Agent must be a single line.")
    values["user_agent"] = user_agent

    for name in ("text_model", "image_model"):
        values[name] = (values.get(name) or "").strip()
        if not values[name]:
            raise HTTPException(400, "%s is required." % name)
    values["image_size"] = (values.get("image_size") or "").strip() or "1024x1024"

    for name in ("api_key", "image_api_key"):
        raw = values.get(name)
        if raw is None:
            continue
        raw = raw.strip()
        if len(raw) > 1000 or any(ord(char) < 32 for char in raw):
            raise HTTPException(400, "%s contains invalid characters." % name)
        values[name] = raw or None
    return values


# -- payloads -------------------------------------------------------------
class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=500)


class ChatIdIn(BaseModel):
    chat_id: str = Field(default="default", pattern=r"^[A-Za-z0-9:_-]{1,100}$")


class ChatAttachmentIn(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=200)
    mime: str = Field(default="", max_length=120)
    kind: str = Field(default="", max_length=20)
    size: int = Field(default=0, ge=0)


class ChatIn(BaseModel):
    message: str = Field(default="", max_length=8000)
    chat_id: str = Field(default="default", pattern=r"^[A-Za-z0-9:_-]{1,100}$")
    agent_id: Optional[int] = Field(default=None, ge=1)
    attachments: List[ChatAttachmentIn] = Field(
        default_factory=list, max_length=MAX_UPLOAD_FILES
    )


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=8000)
    kind: str = Field(pattern=r"^(once|interval|cron)$")
    schedule: str = Field(min_length=1, max_length=200)
    timezone: Optional[str] = Field(default=None, max_length=80)
    agent_id: Optional[int] = Field(default=None, ge=1)


class TeamAgentIn(BaseModel):
    brief: str = Field(min_length=5, max_length=8000)
    name: Optional[str] = Field(default=None, max_length=60)
    role: Optional[str] = Field(default=None, max_length=200)
    model: Optional[str] = Field(default=None, max_length=160)
    trusted_commands: Optional[List[str]] = Field(default=None, max_length=50)


class TeamAgentUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)
    role: Optional[str] = Field(default=None, max_length=200)
    instructions: Optional[str] = Field(default=None, max_length=8000)
    model: Optional[str] = Field(default=None, max_length=160)
    trusted_commands: Optional[List[str]] = Field(default=None, max_length=50)
    status: Optional[str] = Field(
        default=None, pattern=r"^(active|paused|archived)$"
    )


class TeamTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=8000)
    agent_id: int = Field(ge=1)
    parent_task_id: Optional[int] = Field(default=None, ge=1)


class TeamMessageIn(BaseModel):
    to_agent_id: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=8000)
    task_id: Optional[int] = Field(default=None, ge=1)


class TeamGroupMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class VaultInitIn(BaseModel):
    master_password: str = Field(min_length=10, max_length=500)


class VaultUnlockIn(BaseModel):
    master_password: str = Field(min_length=1, max_length=500)


class VaultEntryIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    domains: List[str] = Field(min_length=1)
    username: str = Field(default="", max_length=500)
    password: str = Field(min_length=1, max_length=2000)
    url: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=4000)
    totp_secret: str = Field(default="", max_length=500)


class VaultEntryUpdateIn(BaseModel):
    label: Optional[str] = Field(default=None, max_length=80)
    domains: Optional[List[str]] = None
    username: Optional[str] = Field(default=None, max_length=500)
    password: Optional[str] = Field(default=None, max_length=2000)
    url: Optional[str] = Field(default=None, max_length=2000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    totp_secret: Optional[str] = Field(default=None, max_length=500)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=500)
    new_password: str = Field(min_length=10, max_length=500)


class SettingsIn(BaseModel):
    agent_name: Optional[str] = Field(default=None, max_length=60)
    agent_style: Optional[str] = Field(default=None, max_length=400)
    write_enabled: Optional[bool] = None
    auto_approve_writes: Optional[bool] = None
    auto_approve_browser: Optional[bool] = None
    shell_enabled: Optional[bool] = None
    browser_allow_private: Optional[bool] = None
    timezone: Optional[str] = Field(default=None, max_length=80)


class BrowserControlIn(BaseModel):
    mode: str = Field(pattern=r"^(take|release)$")


class BrowserOpenIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    session: str = Field(default="default", max_length=100)
    focus: bool = False


class BrowserSelectIn(BaseModel):
    session: str = Field(default="default", max_length=100)


class BrowserClearSiteIn(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    session: str = Field(default="default", max_length=100)


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class TrustedCommandIn(BaseModel):
    command: Optional[str] = Field(default=None, max_length=12_000)
    family: Optional[str] = Field(default=None, max_length=40)


class ApprovalApproveIn(BaseModel):
    scope: str = Field(default="once", pattern=r"^(once|host|command)$")
    pattern: Optional[str] = Field(default=None, max_length=500)


class ProviderIn(BaseModel):
    preset: str = Field(default="custom", max_length=40)
    base_url: Optional[str] = Field(default=None, max_length=2048)
    text_model: str = Field(default="gpt-4o-mini", min_length=1, max_length=160)
    user_agent: str = Field(default="Muse/0.1.0", max_length=200)
    session_header: str = Field(default="", max_length=64)
    image_base_url: Optional[str] = Field(default=None, max_length=2048)
    image_model: str = Field(default="gpt-image-1", max_length=160)
    image_size: str = Field(default="1024x1024", max_length=32)
    api_key: Optional[str] = Field(default=None, max_length=1000)
    image_api_key: Optional[str] = Field(default=None, max_length=1000)
    clear_api_key: bool = False
    clear_image_api_key: bool = False


class ProviderModelsIn(BaseModel):
    preset: Optional[str] = Field(default=None, max_length=40)
    base_url: Optional[str] = Field(default=None, max_length=2048)
    user_agent: Optional[str] = Field(default=None, max_length=200)
    session_header: Optional[str] = Field(default=None, max_length=64)
    api_key: Optional[str] = Field(default=None, max_length=1000)


class CredentialFulfillIn(BaseModel):
    label: str = Field(default="", max_length=80)
    username: str = Field(default="", max_length=500)
    password: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=4000)
    totp_secret: str = Field(default="", max_length=500)
    otp_code: str = Field(default="", max_length=20)
    trust_host: bool = False


class CredentialCancelIn(BaseModel):
    reason: str = Field(default="", max_length=500)


# -- auth ----------------------------------------------------------------
@router.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.post("/api/login")
async def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    services: Services = Depends(get_services),
) -> Dict[str, Any]:
    key = _client(request)
    if not login_limiter.allow(key):
        raise HTTPException(
            429, "Too many attempts. Retry in %ss." % login_limiter.retry_after(key)
        )
    if not password_matches(payload.password, services.settings.app_password):
        services.audit.log("user", "login", {"ip": key}, "failed")
        raise HTTPException(401, "Incorrect password.")
    login_limiter.reset(key)
    token = create_session(
        services.settings.session_secret, services.settings.session_ttl_seconds
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=services.settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=bool(services.settings.cookie_secure),
        path="/",
    )
    services.audit.log("user", "login", {"ip": key}, "ok")
    return {"ok": True}


@router.post("/api/logout")
async def logout(
    request: Request,
    response: Response,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    services.audit.log("user", "logout", _client(request))
    return {"ok": True}


@router.get("/api/session")
async def session_info(
    request: Request, services: Services = Depends(get_services)
) -> Dict[str, Any]:
    authenticated = verify_session(
        request.cookies.get(COOKIE_NAME), services.settings.session_secret
    )
    data: Dict[str, Any] = {
        "authenticated": authenticated,
        "version": __version__,
        "build": BUILD,
        "openai_configured": services.openai.configured,
    }
    if authenticated:
        prefs = services.prefs.all()
        provider = services.providers.public()
        data.update(
            {
                "agent_name": prefs["agent_name"],
                "agent_style": prefs["agent_style"],
                "write_enabled": bool(prefs["write_enabled"]),
                "auto_approve_writes": bool(prefs["auto_approve_writes"]),
                "shell_enabled": bool(prefs["shell_enabled"]),
                "timezone": prefs["timezone"],
                "browser_enabled": services.browser.enabled,
                "provider": {
                    "configured": provider["configured"],
                    "preset": provider["preset"],
                    "model": provider["text_model"],
                    "base_url": provider["base_url"],
                    "source": provider["source"],
                },
            }
        )
    return data


@router.post("/api/uploads")
async def upload_file(
    file: UploadFile = File(...),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadError(
                "The uploaded file is too large (max %d MB)."
                % (MAX_UPLOAD_BYTES // (1024 * 1024))
            )
        meta = save_upload(
            services.settings.uploads_dir,
            file.filename or "upload",
            data,
        )
    except UploadError as exc:
        raise HTTPException(400, str(exc))
    services.audit.log(
        "user",
        "file_uploaded",
        {
            "id": meta["id"],
            "name": meta["name"],
            "kind": meta["kind"],
            "size": meta["size"],
        },
    )
    return meta


@router.get("/api/uploads/{stored_id}")
async def get_upload(
    stored_id: str,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> FileResponse:
    try:
        meta = attachment_for(services.settings.uploads_dir, stored_id)
        path = path_for(services.settings.uploads_dir, stored_id)
    except UploadError as exc:
        raise HTTPException(404, str(exc))
    headers = {"Cache-Control": "private, max-age=3600"}
    if meta["kind"] == "document":
        # Never render an uploaded document inline under the Muse origin.
        headers["Content-Disposition"] = "attachment"
    return FileResponse(
        str(path),
        media_type=meta["mime"],
        headers=headers,
    )


def _runner_for_chat(services: Services, chat_id: str) -> Any:
    runner = services.agent
    agent_id, _ = parse_chat_context(chat_id)
    if agent_id:
        try:
            profile = services.team.get_agent(agent_id)
            if profile["status"] == "active":
                runner = Agent(services, profile=profile)
        except TeamError:
            pass
    return runner


def _annotate_shell_prefixes(pending: List[Dict[str, Any]]) -> None:
    """Suggest a trusted prefix for similar pending shell approvals."""
    commands: Dict[str, List[str]] = {}
    for item in pending:
        if item.get("tool") != "run_shell":
            continue
        command = " ".join(
            str((item.get("args") or {}).get("command") or "").split()
        )
        if command:
            commands.setdefault(str(item.get("chat_id") or ""), []).append(command)
    for item in pending:
        if item.get("tool") != "run_shell":
            continue
        command = " ".join(
            str((item.get("args") or {}).get("command") or "").split()
        )
        if not command:
            continue
        shared = shell_trusted_prefix(commands.get(str(item.get("chat_id") or ""), []))
        item["suggested_prefix"] = shared or command


def _trust_shell_pattern(services: Services, pattern: str) -> None:
    """Add a trusted shell command or prefix (``...*``) for future runs."""
    values = list(services.prefs.get("trusted_shell_commands") or [])
    if pattern in values:
        return
    values.append(pattern)
    services.prefs.update({"trusted_shell_commands": values[-200:]})
    services.audit.log("user", "trusted_shell_prefix", pattern)


def _remember_shell_command(services: Services, approval: Dict[str, Any]) -> None:
    if approval.get("tool") != "run_shell":
        return
    command = str((approval.get("args") or {}).get("command") or "")
    signature = shell_command_signature(command)
    if not signature:
        return
    values = list(services.prefs.get("trusted_shell_commands") or [])
    if signature in values:
        return
    values.append(signature)
    services.prefs.update({"trusted_shell_commands": values[-200:]})
    services.audit.log("user", "trusted_shell_command", signature)
    family = shell_command_family(command)
    if family:
        families = list(services.prefs.get("trusted_shell_families") or [])
        if family not in families:
            families.append(family)
            services.prefs.update(
                {
                    "trusted_shell_families": families,
                    "trusted_shell_families_initialized": True,
                }
            )
            services.audit.log("user", "trusted_shell_family", family)


def _chat_browser_session(chat_id: str) -> str:
    agent_id, _ = parse_chat_context(chat_id)
    return "agent:%d" % agent_id if agent_id else "default"


async def _accept_chat_otp(
    services: Services, chat_id: str, message: str
) -> Optional[Dict[str, Any]]:
    """Consume an OTP typed in chat without storing or echoing the code."""
    try:
        result = await services.browser.submit_chat_otp(
            message, session=_chat_browser_session(chat_id)
        )
    except Exception:
        return None
    if result is None:
        return None
    if result.get("submitted"):
        domain = (urlparse(str(result.get("url") or "")).hostname or "").lower()
        if domain:
            for request in services.credentials.pending(chat_id):
                if str(request.get("kind") or "") != "otp":
                    continue
                if not host_matches(domain, [request.get("domain") or ""]):
                    continue
                try:
                    services.credentials.resolve(
                        int(request["id"]),
                        "fulfilled",
                        None,
                        "One-time code submitted in chat; it was not stored.",
                    )
                except CredentialRequestError:
                    pass
    return result


def _chat_attachments(
    services: Services, payload: ChatIn
) -> List[Dict[str, Any]]:
    if len(payload.attachments) > MAX_UPLOAD_FILES:
        raise HTTPException(
            400, "Too many attachments (max %d)." % MAX_UPLOAD_FILES
        )
    attachments: List[Dict[str, Any]] = []
    total = 0
    for item in payload.attachments:
        try:
            attachment = attachment_from_input(
                services.settings.uploads_dir, item.model_dump()
            )
        except UploadError as exc:
            raise HTTPException(400, str(exc))
        total += int(attachment["size"])
        attachments.append(attachment)
    if total > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Attachments are too large in total.")
    return attachments


# -- chat -----------------------------------------------------------------
@router.post("/api/chat")
async def chat(
    payload: ChatIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    attachments = _chat_attachments(services, payload)
    if not payload.message.strip() and not attachments:
        raise HTTPException(422, "Write a message or attach a file.")
    message = payload.message
    otp_result = None
    if not attachments:
        otp_result = await _accept_chat_otp(
            services, payload.chat_id, payload.message
        )
    otp_handled = otp_result is not None
    if otp_handled:
        if otp_result.get("submitted"):
            message = (
                "The user submitted the one-time code in chat. The browser "
                "accepted it; continue from the current page and report the "
                "result without repeating the code."
            )
        else:
            message = (
                "The user submitted a one-time code in chat, but the browser "
                "could not accept it: %s. Continue and tell the user plainly, "
                "without repeating the code."
                % otp_result.get("error", "unknown error")
            )
    runner = services.agent
    if payload.agent_id:
        try:
            profile = services.team.get_agent(payload.agent_id)
        except TeamError as exc:
            raise _team_http_error(exc)
        if profile["status"] != "active":
            raise HTTPException(400, "That agent is not active.")
        runner = Agent(services, profile=profile)
    queue = getattr(services, "chat_queue", None)
    started = False
    if queue is not None:
        started = queue.try_begin(payload.chat_id)
        if not started:
            services.agent.record_user(
                payload.chat_id, message, attachments
            )
            if attachments:
                queue.enqueue_message(
                    payload.chat_id, message, attachments
                )
            else:
                queue.enqueue(payload.chat_id, message)
            return {
                "chat_id": payload.chat_id,
                "queued": True,
                "reply": "",
                "steps": [],
                "images": [],
                "pending_approvals": [],
                "pending_credentials": [],
                "otp_handled": otp_handled,
                "otp_submitted": bool(otp_result and otp_result.get("submitted")),
            }
    try:
        timeout = float(services.settings.agent_run_timeout_seconds)
        if timeout > 0:
            result = await asyncio.wait_for(
                runner.run(
                    chat_id=payload.chat_id,
                    user_text=message,
                    attachments=attachments,
                ),
                timeout=timeout,
            )
        else:
            result = await runner.run(
                chat_id=payload.chat_id,
                user_text=message,
                attachments=attachments,
            )
        result["otp_handled"] = otp_handled
        result["otp_submitted"] = bool(otp_result and otp_result.get("submitted"))
        return result
    except asyncio.TimeoutError:
        if getattr(services, "cancellations", None) is not None:
            services.cancellations.cancel(payload.chat_id)
        raise HTTPException(
            504,
            "The agent run exceeded %d seconds and was stopped. "
            "Use Stop for long runs or narrow the request."
            % int(services.settings.agent_run_timeout_seconds),
        )
    except AgentError as exc:
        message = str(exc)
        raise HTTPException(
            400 if "OPENAI_API_KEY" in message else 502, message
        )
    finally:
        if queue is not None and started:
            queue.finish(payload.chat_id)


@router.post("/api/chat/cancel")
async def cancel_chat(
    payload: ChatIdIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    cancelled = False
    if getattr(services, "cancellations", None) is not None:
        cancelled = services.cancellations.cancel(payload.chat_id)
    return {"cancelled": cancelled}


@router.get("/api/chat/latest")
async def chat_latest(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Newest message per visible chat, so the UI can flag background posts."""
    rows = services.db.query(
        "SELECT m.chat_id, m.id, m.role, m.created_at FROM messages m "
        "JOIN (SELECT chat_id, MAX(id) AS id FROM messages "
        "      WHERE chat_id = 'default' OR chat_id GLOB 'agent:*:dm' "
        "      GROUP BY chat_id) latest ON latest.id = m.id "
        "ORDER BY m.id DESC"
    )
    return {"chats": rows}


@router.get("/api/chat/history")
async def chat_history(
    chat_id: str = Query("default", pattern=r"^[A-Za-z0-9:_-]{1,100}$"),
    limit: int = Query(100, ge=1, le=300),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {
        "chat_id": chat_id,
        "messages": services.agent.history_for_ui(chat_id, limit),
    }


@router.get("/api/chat/status")
async def chat_status(
    chat_id: str = Query("default", pattern=r"^[A-Za-z0-9:_-]{1,100}$"),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    queue = getattr(services, "chat_queue", None)
    if queue is None:
        return {"chat_id": chat_id, "active": False, "queued": 0}
    result = queue.status(chat_id)
    result["chat_id"] = chat_id
    return result


@router.post("/api/chat/clear")
async def chat_clear(
    payload: ChatIdIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    services.agent.clear(payload.chat_id)
    return {"ok": True}


# -- team -----------------------------------------------------------------
@router.get("/api/team")
async def team_snapshot(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return services.team.snapshot()


@router.get("/api/team/summary")
async def team_summary(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return services.team.summary()


@router.post("/api/team/agents")
async def create_team_agent(
    payload: TeamAgentIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        agent = await services.team.create_from_prompt(
            payload.brief,
            name=payload.name,
            role=payload.role,
            model=payload.model,
            trusted_commands=payload.trusted_commands,
        )
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"agent": agent}


@router.get("/api/team/agents")
async def list_team_agents(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {"agents": services.team.list_agents()}


def _owner_scope(services: Services, agent: str) -> str:
    """Map the Team tab's ``agent`` selector to a private memory/skill scope."""
    raw = str(agent or "").strip()
    lead_aliases = {"", "lead", "team lead", "muse", "owner", "0"}
    lead_name = str(services.team.lead_name() or "").strip().lower()
    if raw.lower() in lead_aliases or (lead_name and raw.lower() == lead_name):
        return LEAD_SCOPE
    try:
        agent_id = int(raw)
    except (TypeError, ValueError):
        agent_id = 0
    try:
        profile = (
            services.team.get_agent(agent_id)
            if agent_id
            else services.team.get_agent(name=raw)
        )
    except TeamError:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    return scope_for_agent(int(profile["id"]))


class SkillIn(BaseModel):
    agent: str = Field(default="", max_length=80)
    name: str = Field(min_length=1, max_length=80)
    summary: str = Field(default="", max_length=200)
    instructions: str = Field(min_length=1, max_length=8000)


class SkillUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    summary: Optional[str] = Field(default=None, max_length=200)
    instructions: Optional[str] = Field(default=None, max_length=8000)


@router.get("/api/team/memory")
async def list_team_memory(
    agent: str = Query(default=""),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    scope = _owner_scope(services, agent)
    return {"scope": scope, "memories": services.memories.list_scope(scope, 200)}


@router.delete("/api/team/memory/{memory_id}")
async def delete_team_memory(
    memory_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    if not services.memories.delete(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")
    services.audit.log("user", "memory_delete", {"memory_id": memory_id})
    return {"ok": True}


@router.get("/api/team/skills")
async def list_team_skills(
    agent: str = Query(default=""),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    scope = _owner_scope(services, agent)
    return {"scope": scope, "skills": services.skills.list(scope, 100)}


@router.get("/api/team/skills/{skill_id}")
async def get_team_skill(
    skill_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    skill = services.skills.get_by_id(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found.")
    return {"skill": skill}


@router.post("/api/team/skills")
async def create_team_skill(
    payload: SkillIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    scope = _owner_scope(services, payload.agent)
    try:
        skill = services.skills.save(
            scope, payload.name, payload.instructions, payload.summary
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    services.audit.log(
        "user", "skill_save", {"scope": scope, "name": skill.get("name")}
    )
    return {"skill": skill}


@router.put("/api/team/skills/{skill_id}")
async def update_team_skill(
    skill_id: int,
    payload: SkillUpdateIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    current = services.skills.get_by_id(skill_id)
    if not current:
        raise HTTPException(status_code=404, detail="Skill not found.")
    try:
        skill = services.skills.update(
            skill_id, payload.model_dump(exclude_unset=True)
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409, detail="That owner already has a skill with this name."
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found.")
    services.audit.log(
        "user", "skill_update", {"scope": skill["scope"], "name": skill["name"]}
    )
    return {"skill": skill}


@router.delete("/api/team/skills/{skill_id}")
async def delete_team_skill(
    skill_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    current = services.skills.get_by_id(skill_id)
    if not current:
        raise HTTPException(status_code=404, detail="Skill not found.")
    services.skills.delete_by_id(skill_id)
    services.audit.log(
        "user", "skill_delete", {"scope": current["scope"], "name": current["name"]}
    )
    return {"ok": True}


@router.patch("/api/team/agents/{agent_id}")
async def update_team_agent(
    agent_id: int,
    payload: TeamAgentUpdateIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        agent = services.team.update_agent(agent_id, payload.model_dump())
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"agent": agent}


@router.delete("/api/team/agents/{agent_id}")
async def archive_team_agent(
    agent_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        agent = services.team.archive_agent(agent_id)
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"agent": agent}


@router.get("/api/team/agents/{agent_id}/inbox")
async def team_inbox(
    agent_id: int,
    unread_only: bool = Query(False),
    mark_read: bool = Query(False),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        messages = services.team.inbox(
            agent_id, unread_only=unread_only, mark_read=mark_read
        )
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"messages": messages}


@router.get("/api/team/tasks")
async def list_team_tasks(
    status: Optional[str] = Query(None, max_length=40),
    agent_id: Optional[int] = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {
        "tasks": services.team.list_tasks(
            limit=limit, agent_id=agent_id, status=status
        )
    }


@router.post("/api/team/tasks")
async def create_team_task(
    payload: TeamTaskIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        task = services.team.create_task(
            payload.title,
            payload.prompt,
            payload.agent_id,
            parent_task_id=payload.parent_task_id,
            actor="user",
        )
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"task": task}

@router.get("/api/team/tasks/{task_id}")
async def get_team_task(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        task = services.team.get_task(task_id)
        messages = services.team.task_thread(task_id)
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"task": task, "messages": messages}


@router.post("/api/team/tasks/{task_id}/run")
async def run_team_task(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        task = await services.team_runner.run_task(task_id)
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"task": task}


@router.post("/api/team/tasks/{task_id}/cancel")
async def cancel_team_task(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        task = services.team.cancel_task(task_id)
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"task": task}


@router.get("/api/team/groups")
async def list_team_groups(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {"groups": services.team.list_groups()}


@router.get("/api/team/groups/{agent_a}/{agent_b}")
async def get_team_group(
    agent_a: int,
    agent_b: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        group = services.team.group_info(agent_a, agent_b)
        messages = services.team.group_messages(agent_a, agent_b)
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"group": group, "messages": messages}


@router.post("/api/team/groups/{agent_a}/{agent_b}/messages")
async def send_group_message(
    agent_a: int,
    agent_b: int,
    payload: TeamGroupMessageIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        message = services.team.send_group_message(
            agent_a, agent_b, payload.content
        )
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"message": message}



@router.post("/api/team/messages")
async def send_team_message(
    payload: TeamMessageIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        message = services.team.send_message(
            from_agent_id=None,
            to_agent_id=payload.to_agent_id,
            content=payload.content,
            task_id=payload.task_id,
            kind="message",
            actor="user",
        )
    except TeamError as exc:
        raise _team_http_error(exc)
    return {"message": message}



# -- approvals ------------------------------------------------------------
@router.get("/api/approvals")
async def list_approvals(
    trusted: bool = False,
    brief: bool = False,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    if brief:
        return {
            "pending_count": len(services.approvals.pending(limit=200)),
            "pending_credential_count": len(
                services.credentials.pending(limit=200)
            ),
        }
    data = {
        "pending": services.approvals.pending(),
        "recent": services.approvals.recent(25),
        "pending_credentials": services.credentials.pending(),
        "recent_credentials": services.credentials.recent(10),
        "trusted_hosts": services.trust.list(),
        "vault": services.vault.status(),
    }
    _annotate_shell_prefixes(data["pending"])
    if trusted:
        commands = services.prefs.get("trusted_shell_commands") or []
        families = list(services.prefs.get("trusted_shell_families") or [])
        if not services.prefs.get("trusted_shell_families_initialized"):
            for item in commands:
                family = shell_command_family(item)
                if family and family not in families:
                    families.append(family)
            services.prefs.update(
                {
                    "trusted_shell_families": families,
                    "trusted_shell_families_initialized": True,
                }
            )
        data["trusted_commands"] = commands
        data["trusted_shell_families"] = families
    else:
        data["trusted_command_count"] = len(
            services.prefs.get("trusted_shell_commands") or []
        )
    return data


@router.post("/api/approvals/{approval_id}/approve")
async def approve(
    approval_id: int,
    payload: Optional[ApprovalApproveIn] = None,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        approval = services.approvals.get(approval_id)
    except ApprovalError as exc:
        raise HTTPException(404, str(exc))
    if approval["status"] != "pending":
        raise HTTPException(409, "This approval is already resolved.")
    scope = payload.scope if payload else "once"
    if scope == "host" and not approval.get("host"):
        raise HTTPException(
            400, "This action is not tied to a browser host, so it can only be approved once."
        )
    trust_pattern = ""
    if scope == "command":
        if approval.get("tool") != "run_shell":
            raise HTTPException(
                400, "Only shell approvals can be trusted by command prefix."
            )
        command = " ".join(
            str((approval.get("args") or {}).get("command") or "").split()
        )
        trust_pattern = " ".join(str(payload.pattern or "").split()) if payload else ""
        if not command or not trust_pattern:
            raise HTTPException(400, "A command prefix is required.")
        if trust_pattern.endswith("*"):
            base = trust_pattern[:-1].strip()
            if not base or not command.startswith(base):
                raise HTTPException(
                    400, "That prefix does not match the command being approved."
                )
        elif command != trust_pattern:
            raise HTTPException(
                400, "That prefix does not match the command being approved."
            )
    if not services.approvals.claim(approval_id):
        raise HTTPException(409, "This approval is already being handled.")
    if scope == "host":
        services.trust.trust(approval["host"])
    if trust_pattern:
        _trust_shell_pattern(services, trust_pattern)

    result = await execute_approved(services, approval)
    _remember_shell_command(services, approval)
    inner = result.get("result") if isinstance(result, dict) else None
    rendered = str(inner if inner is not None else result)[:1500]
    if isinstance(inner, dict) and inner.get("image_url"):
        services.agent.attach(
            approval["chat_id"],
            "Image generated from an approved action.",
            [
                {
                    "image_url": inner["image_url"],
                    "artifact_id": inner.get("artifact_id"),
                }
            ],
        )
    else:
        services.agent.note(
            approval["chat_id"],
            "Approved action %s%s was executed. Result: %s%s"
            % (
                approval["tool"],
                (" on %s" % approval["host"]) if approval.get("host") else "",
                rendered,
                (
                    " Trusted %s for future browser actions."
                    % approval["host"]
                    if scope == "host"
                    else " Trusted shell commands starting with: %s" % trust_pattern
                    if trust_pattern
                    else ""
                ),
            ),
        )
    followup = None
    followup_error = ""
    try:
        runner = _runner_for_chat(services, approval["chat_id"])
        followup = await runner.run(
            chat_id=approval["chat_id"],
            user_text=(
                "The user approved and Muse executed %s%s. Result: %s. "
                "Continue the task from here and report the outcome. Do not ask "
                "the user to type continue."
                % (
                    approval["tool"],
                    (" on %s" % approval["host"]) if approval.get("host") else "",
                    rendered,
                )
            ),
            actor="user",
            store_user=False,
        )
    except Exception as exc:
        followup_error = str(exc)
    return {
        "approval": services.approvals.get(approval_id),
        "result": result,
        "followup": followup,
        "followup_error": followup_error,
        "trusted_hosts": services.trust.list(),
        "trusted_shell_pattern": trust_pattern,
    }


@router.get("/api/trusted-hosts")
async def list_trusted_hosts(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {"trusted_hosts": services.trust.list()}


@router.delete("/api/trusted-hosts/{host}")
async def revoke_trusted_host(
    host: str,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        hosts = services.trust.revoke(host)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"trusted_hosts": hosts}


@router.post("/api/trusted-commands/revoke")
async def revoke_trusted_command(
    payload: TrustedCommandIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    if payload.family:
        family = payload.family.strip().lower()
        if family != "curl":
            raise HTTPException(400, "Unknown trusted command family.")
        families = [
            item
            for item in (services.prefs.get("trusted_shell_families") or [])
            if item != family
        ]
        services.prefs.update(
            {
                "trusted_shell_families": families,
                "trusted_shell_families_initialized": True,
            }
        )
        services.audit.log("user", "trusted_shell_family_revoked", family)
        return {"trusted_shell_families": families}
    if not payload.command:
        raise HTTPException(400, "A command is required.")
    signature = shell_command_signature(payload.command)
    values = [
        item
        for item in (services.prefs.get("trusted_shell_commands") or [])
        if item != signature
    ]
    services.prefs.update({"trusted_shell_commands": values})
    services.audit.log("user", "trusted_shell_command_revoked", signature)
    return {"trusted_commands": values}


@router.post("/api/approvals/{approval_id}/reject")
async def reject(
    approval_id: int,
    payload: RejectIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        approval = services.approvals.get(approval_id)
    except ApprovalError as exc:
        raise HTTPException(404, str(exc))
    if approval["status"] != "pending":
        raise HTTPException(409, "This approval is already resolved.")
    if not services.approvals.claim(approval_id):
        raise HTTPException(409, "This approval is already being handled.")
    services.approvals.resolve(
        approval_id, "rejected", payload.reason or "Rejected by user."
    )
    services.agent.note(
        approval["chat_id"],
        "The user rejected %s%s."
        % (
            approval["tool"],
            (" (%s)" % payload.reason) if payload.reason else "",
        ),
    )
    return {"approval": services.approvals.get(approval_id)}


# -- tasks ----------------------------------------------------------------
@router.get("/api/tasks")
async def list_tasks(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {"tasks": services.scheduler.list_tasks()}


@router.post("/api/tasks")
async def create_task(
    payload: TaskIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    if payload.agent_id is not None:
        try:
            agent = services.team.get_agent(int(payload.agent_id))
        except TeamError as exc:
            raise _team_http_error(exc)
        if agent["status"] != "active":
            raise HTTPException(400, "That agent is not active.")
    try:
        task = services.scheduler.create(
            payload.title,
            payload.prompt,
            payload.kind,
            payload.schedule,
            payload.timezone,
            agent_id=payload.agent_id,
        )
    except ScheduleError as exc:
        raise HTTPException(400, str(exc))
    return {"task": task}


@router.post("/api/tasks/{task_id}/run")
async def run_task(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        return {"task": services.scheduler.trigger_now(task_id)}
    except ScheduleError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/tasks/{task_id}/pause")
async def pause_task(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        return {"task": services.scheduler.set_active(task_id, False)}
    except ScheduleError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/tasks/{task_id}/resume")
async def resume_task(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        return {"task": services.scheduler.set_active(task_id, True)}
    except ScheduleError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/tasks/{task_id}")
async def delete_task(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        services.scheduler.delete(task_id)
    except ScheduleError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@router.get("/api/tasks/{task_id}/runs")
async def task_runs(
    task_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {"runs": services.scheduler.runs(task_id)}


# -- vault ----------------------------------------------------------------
@router.get("/api/vault/status")
async def vault_status(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return services.vault.status()


@router.post("/api/vault/initialize")
async def vault_initialize(
    payload: VaultInitIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        services.vault.initialize(payload.master_password)
    except Exception as exc:
        raise _vault_http_error(exc)
    return services.vault.status()


@router.post("/api/vault/unlock")
async def vault_unlock(
    payload: VaultUnlockIn,
    request: Request,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    key = _client(request) + ":vault"
    if not vault_limiter.allow(key):
        raise HTTPException(
            429, "Too many attempts. Retry in %ss." % vault_limiter.retry_after(key)
        )
    try:
        services.vault.unlock(payload.master_password)
    except InvalidMasterPassword as exc:
        raise HTTPException(403, str(exc))
    except VaultError as exc:
        raise _vault_http_error(exc)
    vault_limiter.reset(key)
    return services.vault.status()


@router.post("/api/vault/lock")
async def vault_lock(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    services.vault.lock()
    services.audit.log("user", "vault_lock", "vault locked")
    return services.vault.status()


@router.get("/api/vault/entries")
async def vault_entries(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {
        "status": services.vault.status(),
        "entries": services.vault.list_entries(),
    }


@router.post("/api/vault/entries")
async def vault_add_entry(
    payload: VaultEntryIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        entry = services.vault.add_entry(
            payload.label,
            payload.domains,
            username=payload.username,
            password=payload.password,
            url=payload.url,
            notes=payload.notes,
            totp_secret=payload.totp_secret,
        )
    except Exception as exc:
        raise _vault_http_error(exc)
    return {"entry": entry}


@router.put("/api/vault/entries/{entry_id}")
async def vault_update_entry(
    entry_id: int,
    payload: VaultEntryUpdateIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        entry = services.vault.update_entry(
            entry_id, **payload.model_dump(exclude_unset=True)
        )
    except Exception as exc:
        raise _vault_http_error(exc)
    return {"entry": entry}


@router.delete("/api/vault/entries/{entry_id}")
async def vault_delete_entry(
    entry_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        services.vault.delete_entry(entry_id)
    except Exception as exc:
        raise _vault_http_error(exc)
    return {"ok": True}


@router.post("/api/vault/entries/{entry_id}/reveal")
async def vault_reveal(
    entry_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        return {"secret": services.vault.reveal(entry_id)}
    except Exception as exc:
        raise _vault_http_error(exc)


@router.post("/api/vault/change-password")
async def vault_change_password(
    payload: ChangePasswordIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        services.vault.change_master_password(
            payload.old_password, payload.new_password
        )
    except Exception as exc:
        raise _vault_http_error(exc)
    return services.vault.status()


# -- credential requests ---------------------------------------------------
@router.get("/api/credential-requests")
async def list_credential_requests(
    chat_id: Optional[str] = Query(None, max_length=80),
    status: str = Query("pending", pattern=r"^(pending|recent)$"),
    limit: int = Query(50, ge=1, le=200),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    if status == "pending":
        requests = services.credentials.pending(chat_id, limit)
    else:
        requests = services.credentials.recent(limit)
    return {"requests": requests, "vault": services.vault.status()}


@router.post("/api/credential-requests/{request_id}/fulfill")
async def fulfill_credential_request(
    request_id: int,
    payload: CredentialFulfillIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        request = services.credentials.get(request_id)
    except CredentialRequestError as exc:
        raise HTTPException(404, str(exc))
    if request["status"] != "pending":
        raise HTTPException(409, "This credential request is already resolved.")

    kind = str(request.get("kind") or "").strip().lower()
    if not kind:
        kind = "vault" if not request.get("login_url") else "login"
    if kind == "login" and not request.get("login_url"):
        kind = "vault"
    browser_session = _chat_browser_session(request["chat_id"])
    host = (request["domain"] or "").lower()

    if kind != "vault":
        browser_status = services.browser.status(browser_session)
        if not browser_status.get("started") or not browser_status.get("url"):
            raise HTTPException(
                409, "The browser is not on the verification page anymore. Ask Muse to reopen it."
            )
        host = (urlparse(browser_status["url"]).hostname or "").lower()
        if not host or not host_matches(host, [request["domain"]]):
            raise HTTPException(
                409,
                "The browser is no longer on %s. Ask Muse to reopen the page."
                % request["domain"],
            )

    if kind == "otp":
        if not payload.otp_code.strip():
            raise HTTPException(400, "Enter the one-time code.")
        try:
            login = await services.browser.submit_otp(
                payload.otp_code,
                session=browser_session,
                otp_selector=request.get("otp_selector", ""),
                submit_selector=request.get("submit_selector", ""),
            )
        except BrowserUnavailable as exc:
            raise HTTPException(503, str(exc))
        except BrowserError as exc:
            raise HTTPException(409, str(exc))
        services.credentials.resolve(
            request_id,
            "fulfilled",
            None,
            "One-time code submitted; it was not stored.",
        )
        services.agent.note(
            request["chat_id"],
            "The user submitted the one-time code for %s. Continue from the "
            "current page and report the result." % request["domain"],
        )
        followup = None
        followup_error = ""
        try:
            runner = services.agent
            agent_id, _ = parse_chat_context(request["chat_id"])
            if agent_id:
                try:
                    profile = services.team.get_agent(agent_id)
                    if profile["status"] == "active":
                        runner = Agent(services, profile=profile)
                except TeamError:
                    pass
            followup = await runner.run(
                chat_id=request["chat_id"],
                user_text=(
                    "The user submitted the one-time code for %s. Continue "
                    "from the current page and report the result. Original "
                    "reason: %s"
                    % (
                        request["domain"],
                        request["reason"] or "a verification challenge",
                    )
                ),
                actor="user",
            )
        except Exception as exc:
            followup_error = str(exc)
        return {
            "request": services.credentials.get(request_id),
            "entry": None,
            "login": login,
            "login_ok": True,
            "kind": "otp",
            "followup": followup,
            "followup_error": followup_error,
            "trusted_hosts": services.trust.list(),
        }

    vault_status = services.vault.status()
    if not vault_status["initialized"]:
        raise HTTPException(
            409, "Create your vault first (Vault tab), then save this credential."
        )
    if not vault_status["unlocked"]:
        raise HTTPException(
            423, "Unlock the vault first (Vault tab), then save this credential."
        )

    vault_only = kind == "vault"
    if not payload.label.strip() or not payload.password:
        raise HTTPException(400, "Label and password are required.")

    label = payload.label.strip()
    existing = services.db.one(
        "SELECT id, domains FROM vault_entries WHERE label = ?", (label,)
    )
    try:
        if existing:
            domains = [d for d in existing["domains"].split(",") if d]
            if not host_matches(host, domains):
                raise HTTPException(
                    409,
                    "That label is already bound to %s. Choose a different label."
                    % ", ".join(domains),
                )
            entry = services.vault.update_entry(
                int(existing["id"]),
                username=payload.username,
                password=payload.password,
                url=request["login_url"],
                notes=payload.notes or None,
                totp_secret=payload.totp_secret or None,
            )
        else:
            entry = services.vault.add_entry(
                label,
                [request["domain"]],
                username=payload.username,
                password=payload.password,
                url=request["login_url"],
                notes=payload.notes,
                totp_secret=payload.totp_secret,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise _vault_http_error(exc)

    login_ok = False
    login: Dict[str, Any] = {}
    otp_request: Optional[Dict[str, Any]] = None
    if vault_only:
        login = {"mode": "vault"}
        result_text = "Credential saved to the vault; no login was attempted."
    else:
        try:
            login = await services.browser.fill_credential(
                entry["label"],
                username_selector=request["username_selector"],
                password_selector=request["password_selector"],
                submit_selector=request["submit_selector"],
                session=browser_session,
            )
            login_ok = not bool(login.get("otp_error")) and not bool(
                login.get("otp_required")
            )
            if login.get("otp_required") and login.get("otp_selector"):
                otp_request = services.credentials.create(
                    request["chat_id"],
                    entry["label"],
                    host,
                    login.get("url") or request["login_url"],
                    "Muse needs the one-time code shown by the site to continue "
                    "the login.",
                    submit_selector=str(login.get("otp_submit_selector") or "")[:500],
                    kind="otp",
                    otp_selector=str(login.get("otp_selector") or "")[:500],
                )
                login["error"] = "Waiting for a one-time code."
            elif not login_ok:
                login["error"] = login.get("otp_error") or "Login did not complete."
        except Exception as exc:
            login = {"error": "%s: %s" % (type(exc).__name__, exc)}
        result_text = (
            "Signed in on %s" % host
            if login_ok
            else (
                "Credential saved; login is waiting for a one-time code."
                if otp_request
                else "Credential saved; login failed: %s"
                % login.get("error", "unknown error")
            )
        )
    services.credentials.resolve(
        request_id, "fulfilled", int(entry["id"]), result_text
    )
    if payload.trust_host:
        services.trust.trust(request["domain"])
    services.agent.note(
        request["chat_id"],
        (
            "Vault entry '%s' was saved securely for %s. No login was attempted.%s"
            % (
                entry["label"],
                request["domain"],
                (
                    " Trusted %s for future browser actions." % request["domain"]
                    if payload.trust_host
                    else ""
                ),
            )
        )
        if vault_only
        else (
            "Vault entry '%s' was saved and Muse attempted the login on %s. Result: %s%s"
            % (
                entry["label"],
                host,
                result_text,
                (
                    " Trusted %s for future browser actions." % request["domain"]
                    if payload.trust_host
                    else ""
                ),
            )
        ),
    )

    followup = None
    followup_error = ""
    if login_ok:
        try:
            runner = services.agent
            agent_id, _ = parse_chat_context(request["chat_id"])
            if agent_id:
                try:
                    profile = services.team.get_agent(agent_id)
                    if profile["status"] == "active":
                        runner = Agent(services, profile=profile)
                except TeamError:
                    pass
            followup = await runner.run(
                chat_id=request["chat_id"],
                user_text=(
                    "The user saved the vault credential for %s and Muse "
                    "submitted the login. Continue from the current page and "
                    "report the result. Original reason: %s"
                    % (
                        request["domain"],
                        request["reason"] or "a login was required",
                    )
                ),
                actor="user",
            )
        except Exception as exc:
            followup_error = str(exc)

    return {
        "request": services.credentials.get(request_id),
        "entry": {"id": entry["id"], "label": entry["label"]},
        "login": login,
        "login_ok": login_ok,
        "otp_request": otp_request,
        "followup": followup,
        "followup_error": followup_error,
        "trusted_hosts": services.trust.list(),
    }


@router.post("/api/credential-requests/{request_id}/cancel")
async def cancel_credential_request(
    request_id: int,
    payload: CredentialCancelIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        request = services.credentials.get(request_id)
    except CredentialRequestError as exc:
        raise HTTPException(404, str(exc))
    if request["status"] != "pending":
        raise HTTPException(409, "This credential request is already resolved.")
    services.credentials.resolve(
        request_id,
        "cancelled",
        None,
        payload.reason or "Cancelled by user.",
    )
    services.agent.note(
        request["chat_id"],
        "The user cancelled the credential request for %s." % request["domain"],
    )
    return {"request": services.credentials.get(request_id)}


# -- browser --------------------------------------------------------------
@router.get("/api/browser/status")
async def browser_status(
    request: Request,
    session: str = Query("default", max_length=100),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    status = services.browser.status(
        session, request_host=request.headers.get("host")
    )
    control = str(services.prefs.get("browser_control") or "agent")
    status["control"] = control
    status["allow_private_networks"] = bool(
        services.prefs.get("browser_allow_private")
        or services.settings.browser_allow_private_networks
    )
    view_url = status.get("view_url") or ""
    if view_url and control != "user":
        # Until the user takes control the live view is watch-only: noVNC
        # ignores pointer and keyboard input with view_only=1.
        separator = "&" if "?" in view_url else "?"
        status["view_url"] = view_url + separator + "view_only=1"
    return status


@router.post("/api/browser/control")
async def browser_control(
    payload: BrowserControlIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Hand the browser to the human (pausing agent actions) or give it back."""
    mode = "user" if payload.mode == "take" else "agent"
    services.prefs.update({"browser_control": mode})
    services.audit.log("user", "browser_control", {"mode": payload.mode}, "ok")
    return {"control": mode}


@router.get("/api/browser/otp-status")
async def browser_otp_status(
    session: str = Query("default", max_length=100),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    status = services.browser.status(session)
    if not status.get("started") or not status.get("url"):
        return {"required": False, "session": session}
    try:
        fields = await services.browser.detect_otp_fields(session)
    except Exception:
        return {"required": False, "session": session}
    return {
        "required": bool(fields.get("otp")),
        "session": session,
        "url": fields.get("url") or status.get("url") or "",
        "title": fields.get("title") or "",
    }


@router.get("/api/browser/diagnostics")
async def browser_diagnostics(
    request: Request,
    session: str = Query("default", max_length=100),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return services.browser.diagnostics(
        session, request_host=request.headers.get("host")
    )


@router.post("/api/browser/clear-site")
async def browser_clear_site(
    payload: BrowserClearSiteIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        result = await services.browser.clear_site(
            payload.domain, session=payload.session
        )
    except BrowserUnavailable as exc:
        raise HTTPException(503, str(exc))
    except BrowserError as exc:
        raise HTTPException(409, str(exc))
    services.audit.log("user", "browser_site_data_cleared", payload.domain)
    return result


@router.get("/api/browser/screenshot")
async def browser_screenshot(
    session: str = Query("default", max_length=100),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Response:
    try:
        png = await services.browser.screenshot(session)
    except BrowserUnavailable as exc:
        raise HTTPException(503, str(exc))
    except BrowserError as exc:
        raise HTTPException(409, str(exc))
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/browser/select")
async def browser_select(
    payload: BrowserSelectIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        result = await services.browser.activate(payload.session)
    except BrowserUnavailable as exc:
        raise HTTPException(503, str(exc))
    except BrowserError as exc:
        raise HTTPException(409, str(exc))
    return result


@router.post("/api/browser/open")
async def browser_open(
    payload: BrowserOpenIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    try:
        result = await services.browser.open(
            payload.url, session=payload.session, focus=payload.focus
        )
    except BrowserUnavailable as exc:
        raise HTTPException(503, str(exc))
    except (BrowserError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    services.audit.log("user", "browser_open", {"url": payload.url, "session": payload.session})
    return result


# -- artifacts ------------------------------------------------------------
@router.get("/api/artifacts/{artifact_id}/raw")
async def artifact_raw(
    artifact_id: int,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> FileResponse:
    row = services.db.one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if not row:
        raise HTTPException(404, "Artifact not found.")
    root = services.settings.artifacts_dir.resolve()
    path = (root / row["filename"]).resolve()
    if root != path.parent and root not in path.parents:
        raise HTTPException(400, "Invalid artifact path.")
    if not path.exists():
        raise HTTPException(404, "Artifact file is missing.")
    media = (
        "image/png"
        if row["kind"] == "image"
        else "text/markdown; charset=utf-8"
    )
    return FileResponse(
        str(path), media_type=media, headers={"Cache-Control": "private, max-age=3600"}
    )


# -- settings -------------------------------------------------------------
@router.get("/api/settings")
async def read_settings(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    config = services.providers.current()
    return {
        "preferences": services.prefs.all(),
        "limits": {
            "max_agent_steps": services.settings.max_agent_steps,
            "scheduler_timezone": services.settings.scheduler_timezone,
            "browser_allowed_domains": services.settings.allowed_domains,
            "vault_idle_lock_seconds": services.settings.vault_idle_lock_seconds,
            "text_model": config.text_model,
            "image_model": config.image_model,
        },
    }


@router.put("/api/settings")
async def write_settings(
    payload: SettingsIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    values = payload.model_dump(exclude_unset=True)
    if "timezone" in values:
        tz_name = str(values.get("timezone") or "").strip()
        try:
            ZoneInfo(tz_name)
        except Exception:
            raise HTTPException(400, "Unknown timezone.")
        values["timezone"] = tz_name
    if "agent_name" in values:
        name = str(values.get("agent_name") or "").strip()
        if not name:
            raise HTTPException(400, "Agent name cannot be empty.")
        values["agent_name"] = name
    if "agent_style" in values:
        values["agent_style"] = str(values.get("agent_style") or "").strip()
    prefs = services.prefs.update(values)
    services.audit.log("user", "settings_update", values)
    return {"preferences": prefs}


# -- provider -------------------------------------------------------------
@router.get("/api/provider")
async def read_provider(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {
        "provider": services.providers.public(),
        "presets": list(ALLOWED_PRESETS),
    }


@router.put("/api/provider")
async def write_provider(
    payload: ProviderIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    values = _provider_values(payload)
    updated = services.providers.update(values)
    invalidate = getattr(services.openai, "invalidate", None)
    if callable(invalidate):
        invalidate()
    return {"provider": updated}


@router.post("/api/provider/test")
async def test_provider(
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    config = services.providers.current()
    if not config.api_key:
        raise HTTPException(400, "No API key is configured yet.")

    started = time.perf_counter()
    ok = False
    reply = ""
    error = ""
    try:
        response = await services.openai.achat(
            [{"role": "user", "content": "Reply with exactly: MUSE_OK"}],
            session_id="provider-test",
        )
        choices = getattr(response, "choices", None) or []
        if choices:
            reply = (choices[0].message.content or "")[:300]
        ok = True
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    if config.api_key:
        error = error.replace(config.api_key, "[redacted]")
    services.audit.log(
        "user",
        "provider_test",
        {"ok": ok, "model": config.text_model, "latency_ms": latency_ms},
        "ok" if ok else "error",
    )
    return {
        "ok": ok,
        "model": config.text_model,
        "latency_ms": latency_ms,
        "reply": reply,
        "error": error[:1000],
    }


@router.post("/api/provider/models")
async def list_provider_models(
    payload: ProviderModelsIn,
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Query the provider's OpenAI-protocol model discovery (GET /models)."""
    values = payload.model_dump(exclude_unset=True)
    base_url = str(values.get("base_url") or "").strip()
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise HTTPException(400, "base_url must be an http(s) URL with a host.")
        if parsed.username or parsed.password:
            raise HTTPException(400, "base_url must not contain embedded credentials.")
    header = str(values.get("session_header") or "").strip()
    if header and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
        raise HTTPException(400, "Session header must be a valid HTTP header name.")
    user_agent = str(values.get("user_agent") or "").strip()
    if "\r" in user_agent or "\n" in user_agent:
        raise HTTPException(400, "User-Agent must be a single line.")
    api_key = values.get("api_key")
    if api_key is not None:
        api_key = str(api_key).strip()
        if len(api_key) > 1000 or any(ord(char) < 32 for char in api_key):
            raise HTTPException(400, "api_key contains invalid characters.")
    api_key = api_key or None

    if not (api_key or services.providers.current().api_key):
        raise HTTPException(400, "No API key is configured yet.")

    list_models = getattr(services.openai, "list_models", None)
    if not callable(list_models):
        return {
            "ok": False,
            "models": [],
            "error": "Model listing is unavailable in this Muse build.",
            "latency_ms": 0,
        }

    started = time.perf_counter()
    ok = False
    models: List[str] = []
    error = ""
    try:
        models = await list_models(
            base_url=base_url or None,
            api_key=api_key,
            user_agent=user_agent or None,
            session_header=header or None,
        )
        ok = True
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    redact = api_key or services.providers.current().api_key
    if redact:
        error = error.replace(redact, "[redacted]")
    services.audit.log(
        "user",
        "provider_models",
        {"ok": ok, "count": len(models), "latency_ms": latency_ms},
        "ok" if ok else "error",
    )
    if ok:
        return {"ok": True, "models": models, "latency_ms": latency_ms}
    return {"ok": False, "models": [], "error": error[:1000], "latency_ms": latency_ms}


# -- audit ----------------------------------------------------------------
@router.get("/api/activity")
async def activity(
    limit: int = Query(100, ge=1, le=500),
    services: Services = Depends(get_services),
    _: str = Depends(require_auth),
) -> Dict[str, Any]:
    return {
        "audit": services.audit.recent(limit),
        "approvals": services.approvals.recent(min(limit, 50)),
    }