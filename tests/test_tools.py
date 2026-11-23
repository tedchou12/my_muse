from __future__ import annotations

import json

import pytest

from app.tools import execute_tool, summarise


async def test_unknown_tool(app):
    result = await execute_tool(app.state.services, "nope", {}, chat_id="t")
    assert "Unknown tool" in result["error"]


async def test_write_access_gate(app):
    services = app.state.services
    services.prefs.update({"write_enabled": False})
    result = await execute_tool(
        services, "generate_image", {"prompt": "a cat"}, chat_id="t"
    )
    assert "Write access is disabled" in result["error"]


async def test_vault_fill_always_requires_approval(app):
    services = app.state.services
    services.prefs.update({"write_enabled": True, "auto_approve_writes": True})
    assert services.tools["vault_fill"].always_approve is True

    result = await execute_tool(
        services,
        "vault_fill",
        {"label": "GitHub", "password_selector": "#password"},
        chat_id="chat-vault",
    )
    assert result["status"] == "pending_approval"
    approval = services.approvals.get(result["approval_id"])
    assert approval["tool"] == "vault_fill"
    assert approval["chat_id"] == "chat-vault"
    # Only selectors and the label are stored; no secret values.
    assert approval["args"] == {
        "label": "GitHub",
        "password_selector": "#password",
    }


async def test_read_tool_runs_without_approval(app):
    services = app.state.services
    services.prefs.update({"write_enabled": False})
    result = await execute_tool(services, "list_tasks", {}, chat_id="t")
    assert result["ok"] is True
    assert result["result"] == {"tasks": []}


async def test_memory_tools(app):
    services = app.state.services
    stored = await execute_tool(
        services,
        "remember",
        {"fact": "Prefers brief answers"},
        chat_id="t",
    )
    assert stored["ok"] is True
    recalled = await execute_tool(
        services, "recall", {"query": "brief"}, chat_id="t"
    )
    assert recalled["result"]["memories"] == ["Prefers brief answers"]


def test_summaries_redact_secrets():
    summary = summarise("browser_type", {"selector": "#p", "text": "hunter2"})
    assert "Type into #p" == summary
    assert "hunter2" not in summary


def test_redaction_helpers():
    from app.tools import _redact

    clean = _redact({"password": "hunter2", "nested": {"api_key": "sk-1"}})
    assert clean["password"] == "[redacted]"
    assert clean["nested"]["api_key"] == "[redacted]"


def test_approval_claim_and_dedupe(app):
    services = app.state.services
    first = services.approvals.create(
        "c", "browser_click", {"target": "#buy"}, "Click buy"
    )
    second = services.approvals.create(
        "c", "browser_click", {"target": "#buy"}, "Click buy again"
    )
    assert first["id"] == second["id"]
    assert services.approvals.claim(first["id"]) is True
    assert services.approvals.claim(first["id"]) is False
    assert services.approvals.pending() == []

    resolved = services.approvals.resolve(first["id"], "approved", "done")
    assert resolved["status"] == "approved"


async def test_approved_action_respects_read_only(app):
    from app.tools import execute_approved

    services = app.state.services
    approval = services.approvals.create(
        "c", "generate_image", {"prompt": "x"}, "Generate image"
    )
    assert services.approvals.claim(approval["id"]) is True
    services.prefs.update({"write_enabled": False})

    result = await execute_approved(services, services.approvals.get(approval["id"]))
    assert "Write access is disabled" in result["error"]
    assert services.approvals.get(approval["id"])["status"] == "rejected"
