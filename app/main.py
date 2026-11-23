"""FastAPI application factory and process lifecycle.

Run with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent import Agent
from .api import router
from .approvals import ApprovalQueue
from .audit import AuditLog, Preferences
from .browser import BrowserManager
from .cancellation import CancellationRegistry
from .chat_queue import ChatQueue
from .config import Settings
from .credentials import CredentialRequestQueue
from .db import Database
from .openai_client import OpenAIService
from .providers import ProviderStore
from .scheduler import Scheduler
from .services import Services
from .team import TeamRunner, TeamStore
from .tools import ToolKit
from .trust import TrustedHosts
from .vault import Vault

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
logger = logging.getLogger("muse")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

DEFAULT_FRAME_SOURCES = (
    "'self'",
    "http://127.0.0.1:6080",
    "http://localhost:6080",
)


def _frame_origin(value: str) -> str:
    """Return a safe CSP origin for a URL, or an empty string."""
    parsed = urlparse((value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def build_security_headers(settings: Settings) -> Dict[str, str]:
    frame_sources = list(DEFAULT_FRAME_SOURCES)

    def add_source(value: str) -> None:
        origin = _frame_origin(value)
        if origin and origin not in frame_sources:
            frame_sources.append(origin)

    for item in (settings.csp_frame_src or "").replace(",", " ").split():
        add_source(item)
    add_source(settings.browser_vnc_url or "")

    headers = dict(SECURITY_HEADERS)
    headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; "
        "frame-src " + " ".join(frame_sources) + "; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    return headers


def build_services(settings: Settings) -> Services:
    settings.ensure_dirs()
    db = Database(settings.database_path)
    audit = AuditLog(db)
    prefs = Preferences(
        db,
        {
            "write_enabled": settings.write_enabled,
            "auto_approve_writes": settings.auto_approve_writes,
            "shell_enabled": settings.shell_enabled,
            "timezone": settings.scheduler_timezone,
        },
    )
    vault = Vault(db, audit, idle_seconds=settings.vault_idle_lock_seconds)
    browser = BrowserManager(settings, vault)
    providers = ProviderStore(db, settings, audit)
    credentials = CredentialRequestQueue(db, audit)
    trust = TrustedHosts(db, audit)
    openai_service = OpenAIService(settings, providers)
    approvals = ApprovalQueue(db, audit)
    scheduler = Scheduler(
        db,
        audit,
        timezone=settings.scheduler_timezone,
        tick_seconds=settings.scheduler_tick_seconds,
        max_concurrent=settings.scheduler_max_concurrent,
        enabled=settings.scheduler_enabled,
    )
    services = Services(
        settings=settings,
        db=db,
        audit=audit,
        prefs=prefs,
        providers=providers,
        credentials=credentials,
        trust=trust,
        vault=vault,
        browser=browser,
        openai=openai_service,
        approvals=approvals,
        scheduler=scheduler,
    )
    services.team = TeamStore(db, audit, settings, openai_service)
    services.team_runner = TeamRunner(services)
    services.cancellations = CancellationRegistry()
    services.chat_queue = ChatQueue()
    services.tools = ToolKit(services).specs
    services.agent = Agent(services)
    return services


async def _housekeeping(services: Services) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            services.vault.status()  # enforces idle auto-lock
            services.approvals.expire_stale()
            services.credentials.expire_stale()
        except Exception:  # pragma: no cover - background best effort
            logger.exception("Muse housekeeping failed")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate_runtime()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    services = build_services(settings)
    agent: Agent = services.agent

    async def run_scheduled_task(task: Dict[str, Any]) -> str:
        prompt = (
            "Scheduled task: %s\nSchedule: %s\n\n%s\n\n"
            "Carry this out now and report the result. Do not ask follow-up "
            "questions." % (task["title"], task["kind"], task["prompt"])
        )
        result = await agent.run(
            chat_id="task:%d" % task["id"],
            user_text=prompt,
            actor="task:%d" % task["id"],
        )
        return str(result.get("reply", ""))

    services.scheduler.runner = run_scheduled_task

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.audit.log("system", "startup", {"version": __version__})
        housekeeping = asyncio.create_task(_housekeeping(services))
        await services.scheduler.start()
        services.team_runner.start()
        try:
            yield
        finally:
            housekeeping.cancel()
            try:
                await housekeeping
            except asyncio.CancelledError:
                pass
            await services.team_runner.stop()
            await services.scheduler.stop()
            await services.browser.close()
            services.vault.lock()
            services.audit.log("system", "shutdown", {"version": __version__})
            services.db.close()

    app = FastAPI(
        title="Muse",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services
    app.state.agent = agent
    security_headers = build_security_headers(settings)

    @app.middleware("http")
    async def security_headers_middleware(request, call_next):
        response = await call_next(request)
        for key, value in security_headers.items():
            response.headers.setdefault(key, value)
        return response

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app
