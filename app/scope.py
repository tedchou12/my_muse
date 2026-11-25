"""Owner scopes for private memory and skills.

Every durable note or skill belongs to exactly one owner: the team lead
(``lead``) or a named agent (``agent:<id>``). Nothing is shared between
owners, which keeps each agent's prompt small and prevents cross-talk.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

LEAD_SCOPE = "lead"


def scope_for_agent(agent_id: Optional[int]) -> str:
    return LEAD_SCOPE if not agent_id else "agent:%d" % int(agent_id)


def scope_from_chat(chat_id: str) -> str:
    match = re.search(r"(?:^|:)agent:(\d+)", chat_id or "")
    return scope_for_agent(int(match.group(1)) if match else None)


def scope_from_profile(profile: Optional[Dict[str, Any]]) -> str:
    if profile and profile.get("id"):
        try:
            return scope_for_agent(int(profile["id"]))
        except (TypeError, ValueError):
            return LEAD_SCOPE
    return LEAD_SCOPE


def scope_from_source(source: str) -> str:
    """Derive a scope from a legacy memory ``source`` chat id (migration)."""
    return scope_from_chat(source)
