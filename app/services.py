"""Container for the long-lived services shared by routes, agent and scheduler."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .approvals import ApprovalQueue
from .audit import AuditLog, Preferences
from .browser import BrowserManager
from .config import Settings
from .credentials import CredentialRequestQueue
from .db import Database
from .memory import MemoryStore
from .openai_client import OpenAIService
from .providers import ProviderStore
from .scheduler import Scheduler
from .skills import SkillStore
from .trust import TrustedHosts
from .vault import Vault


@dataclass
class Services:
    settings: Settings
    db: Database
    audit: AuditLog
    prefs: Preferences
    providers: ProviderStore
    credentials: CredentialRequestQueue
    trust: TrustedHosts
    vault: Vault
    browser: BrowserManager
    openai: OpenAIService
    approvals: ApprovalQueue
    scheduler: Scheduler
    memories: MemoryStore
    skills: SkillStore
    team: Any = None
    team_runner: Any = None
    cancellations: Any = None
    chat_queue: Any = None
    agent: Any = None
    tools: Dict[str, Any] = field(default_factory=dict)
