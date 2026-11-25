from __future__ import annotations

import json
import os
import time

import pytest

from app.shell import workspace_path
from app.tools import execute_tool, summarise
from conftest import CSRF, FakeMessage, install_fake_ai


async def test_agent_trusted_commands_skip_approval(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "auto_approve_writes": False})
    command = "python3 -m pip install --help"
    agent = services.team.create_agent(
        "Kiosk",
        "Cheap kiosk",
        "Do exactly one thing.",
        trusted_commands=["python3 -m pip install*"],
    )
    chat_id = "agent:%d:dm" % agent["id"]
    allowed = await execute_tool(
        services, "run_shell", {"command": command}, chat_id=chat_id
    )
    assert allowed.get("ok") is True, allowed

    muse = await execute_tool(
        services, "run_shell", {"command": command}, chat_id="default"
    )
    assert muse.get("status") == "pending_approval"


async def test_muse_inbox_receives_task_results(app):
    services = app.state.services
    team = services.team
    agent = team.create_agent("Reporter", "Runner", "Do it.")
    task = team.create_task("Report", "Do it.", agent["id"])
    team.claim_task(task["id"])
    team.complete_task(task["id"], "Everything is fine.")
    rows = team.inbox(None, limit=5)
    assert any("Everything is fine." in row["content"] for row in rows)

    result = await execute_tool(services, "read_inbox", {"limit": 5}, chat_id="default")
    assert result["ok"] is True
    assert result["result"]["inbox"] == "muse"
    assert any(
        "Everything is fine." in item["content"]
        for item in result["result"]["messages"]
    )


async def test_browser_control_pauses_agent_browser(app, auth_client):
    services = app.state.services
    taken = auth_client.post(
        "/api/browser/control", json={"mode": "take"}, headers=CSRF
    )
    assert taken.status_code == 200
    assert taken.json()["control"] == "user"
    assert auth_client.get("/api/browser/status").json()["control"] == "user"

    blocked = await execute_tool(
        services, "browser_open", {"url": "https://example.com"}, chat_id="default"
    )
    assert "manual control" in blocked["error"]

    released = auth_client.post(
        "/api/browser/control", json={"mode": "release"}, headers=CSRF
    )
    assert released.json()["control"] == "agent"


def test_browser_view_is_view_only_until_user_takes_control(app, auth_client):
    services = app.state.services
    services.settings.browser_vnc_url = (
        "http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale"
    )
    status = auth_client.get("/api/browser/status").json()
    assert status["control"] == "agent"
    assert "view_only=1" in status["view_url"]

    auth_client.post(
        "/api/browser/control", json={"mode": "take"}, headers=CSRF
    )
    taken = auth_client.get("/api/browser/status").json()
    assert taken["control"] == "user"
    assert "view_only=1" not in taken["view_url"]


def test_browser_lan_pref_enables_private_network(app, auth_client):
    updated = auth_client.put(
        "/api/settings", json={"browser_allow_private": True}, headers=CSRF
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["preferences"]["browser_allow_private"] is True
    status = auth_client.get("/api/browser/status").json()
    assert status["allow_private_networks"] is True


def _unlocked_vault(services):
    services.vault.initialize("master-password-123")
    services.vault.add_entry(
        "API token",
        ["api.example.com"],
        username="agent-user",
        password="sk-test-super-secret-999",
        notes="notes-value",
    )


async def test_shell_secret_placeholder_is_injected_and_redacted(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "auto_approve_writes": False})
    _unlocked_vault(services)
    command = "printf '%s' {{secret:API token}}"
    services.prefs.update({"trusted_shell_commands": [command]})
    result = await execute_tool(
        services, "run_shell", {"command": command}, chat_id="default"
    )
    assert result["ok"] is True, result
    payload = result["result"]
    assert payload["stdout"] == "[redacted]"
    assert payload["secrets_used"] == 1
    assert "sk-test-super-secret-999" not in json.dumps(result)


async def test_shell_secret_placeholder_requires_approval(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "auto_approve_writes": True})
    _unlocked_vault(services)
    result = await execute_tool(
        services,
        "run_shell",
        {"command": "curl -s https://example.com/{{secret:API token}}"},
        chat_id="default",
    )
    assert result.get("status") == "pending_approval"


async def test_shell_secret_errors_are_actionable(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True, "auto_approve_writes": True})
    services.vault.initialize("master-password-123")
    services.vault.lock()
    command = "printf '%s' {{secret:API token}}"
    services.prefs.update({"trusted_shell_commands": [command]})

    locked = await execute_tool(
        services,
        "run_shell",
        {"command": command},
        chat_id="default",
    )
    assert "locked" in locked["result"]["error"].lower()

    services.vault.unlock("master-password-123")
    services.vault.add_entry(
        "API token", ["api.example.com"], username="u", password="password-123"
    )
    unknown_command = "printf '%s' {{secret:Nope}}"
    services.prefs.update(
        {"trusted_shell_commands": [command, unknown_command]}
    )
    unknown = await execute_tool(
        services,
        "run_shell",
        {"command": unknown_command},
        chat_id="default",
    )
    assert "No vault entry" in unknown["result"]["error"]
    assert "API token" in unknown["result"]["error"]


async def test_shell_secret_field_selector(app):
    services = app.state.services
    services.prefs.update({"shell_enabled": True})
    _unlocked_vault(services)
    command = "printf '%s' {{secret:API token.username}}"
    services.prefs.update({"trusted_shell_commands": [command]})
    result = await execute_tool(
        services, "run_shell", {"command": command}, chat_id="default"
    )
    assert result["ok"] is True, result
    assert result["result"]["stdout"] == "[redacted]"
    assert "agent-user" not in json.dumps(result)


async def test_lead_can_change_agent_model(app):
    services = app.state.services
    services.prefs.update({"auto_approve_writes": True})
    agent = services.team.create_agent(
        "Scout", "Runner", "Do it.", model="qwen3.8-flash"
    )
    result = await execute_tool(
        services,
        "set_agent_model",
        {"agent": "Scout", "model": "beam-free"},
        chat_id="default",
    )
    assert result["ok"] is True, result
    payload = result["result"]
    assert payload["previous_model"] == "qwen3.8-flash"
    assert payload["model"] == "beam-free"
    assert services.team.get_agent(agent["id"])["model"] == "beam-free"

    cleared = await execute_tool(
        services,
        "set_agent_model",
        {"agent": "Scout", "model": ""},
        chat_id="default",
    )
    assert cleared["ok"] is True, cleared
    assert cleared["result"]["model"] == ""
    assert services.team.get_agent(agent["id"])["model"] == ""


async def test_named_agents_cannot_change_models(app):
    services = app.state.services
    services.prefs.update({"auto_approve_writes": True})
    scout = services.team.create_agent("Scout", "Runner", "Do it.")
    scribe = services.team.create_agent("Scribe", "Runner", "Write it.")
    denied = await execute_tool(
        services,
        "set_agent_model",
        {"agent": "Scribe", "model": "beam-free"},
        chat_id="agent:%d:dm" % scout["id"],
    )
    assert "Only the team lead" in denied["error"]
    assert services.team.get_agent(scribe["id"])["model"] == ""

    lead = await execute_tool(
        services,
        "set_agent_model",
        {"agent": "Muse", "model": "beam-free"},
        chat_id="default",
    )
    assert "team lead itself" in lead["error"]


async def test_model_change_needs_approval(app):
    services = app.state.services
    services.prefs.update({"auto_approve_writes": False})
    services.team.create_agent("Scout", "Runner", "Do it.")
    result = await execute_tool(
        services,
        "set_agent_model",
        {"agent": "Scout", "model": "beam-free"},
        chat_id="default",
    )
    assert result.get("status") == "pending_approval"


async def test_convert_time_reports_et_and_jst(app):
    services = app.state.services
    result = await execute_tool(
        services, "convert_time", {"timestamp": 1795471752}, chat_id="default"
    )
    assert result["ok"] is True
    payload = result["result"]
    assert payload["utc"].startswith("2026-")
    assert "ET" in payload["et"] or "EST" in payload["et"] or "EDT" in payload["et"]
    assert "JST" in payload["jst"]
    assert "ET" in payload["note"] and "JST" in payload["note"]


async def test_file_info_flags_stale_files(app):
    services = app.state.services
    base = workspace_path(services.settings)
    target = base / "positions.db"
    target.write_text("fresh", encoding="utf-8")
    old = time.time() - 60 * 86400
    os.utime(target, (old, old))
    result = await execute_tool(
        services, "file_info", {"path": "positions.db"}, chat_id="default"
    )
    assert result["ok"] is True
    info = result["result"]
    assert info["exists"] is True
    assert info["age_human"].endswith("ago")
    assert "stale_warning" in info

    outside = await execute_tool(
        services, "file_info", {"path": "/etc/passwd"}, chat_id="default"
    )
    assert "workspace" in outside.get("error", outside.get("result", {}).get("error", ""))


async def test_schedule_task_carries_chat_context(app):
    install_fake_ai(app, [FakeMessage(content="Tick done.")])
    services = app.state.services
    services.prefs.update({"auto_approve_writes": True})
    services.agent._store(
        "ctx-chat", "user", "The dashboard lives at http://10.0.0.5/phpLiteAdmin"
    )
    result = await execute_tool(
        services,
        "schedule_task",
        {
            "title": "Dashboard watch",
            "prompt": "Check the dashboard and report changes.",
            "kind": "once",
            "schedule": "2030-01-01T00:00",
        },
        chat_id="ctx-chat",
    )
    assert result["ok"] is True, result
    task = services.scheduler.get(result["result"]["task_id"])
    assert "10.0.0.5" in task["context"]

    reply = await services.scheduler.runner(task)
    assert reply == "Tick done."
    first_call = services.openai.chat_calls[0]
    prompt_text = "\n".join(str(item.get("content")) for item in first_call)
    assert "Context captured when this task was scheduled" in prompt_text
    assert "10.0.0.5" in prompt_text


async def test_team_lead_resolves_after_rename(app):
    services = app.state.services
    services.prefs.update({"agent_name": "Smire"})
    agent = services.team.create_agent("Scout", "Runner", "Do it.")
    listed = await execute_tool(services, "list_team", {}, chat_id="default")
    assert listed["ok"] is True
    assert listed["result"]["lead"]["name"] == "Smire"

    sent = await execute_tool(
        services,
        "send_message",
        {"agent": "Smire", "content": "Status: all clear."},
        chat_id="agent:%d:dm" % agent["id"],
    )
    assert sent["ok"] is True, sent
    inbox = services.team.inbox(None, limit=5)
    assert any("all clear" in row["content"] for row in inbox)

    groups = services.team.list_groups()
    assert any(
        group["agent_a_name"] == "Smire" or group["agent_b_name"] == "Smire"
        for group in groups
    )

    denied = await execute_tool(
        services,
        "assign_task",
        {"agent": "Smire", "title": "Nope", "prompt": "Nope"},
        chat_id="agent:%d:dm" % agent["id"],
    )
    assert "not a task assignee" in denied["error"]


async def test_unknown_tool(app):
    result = await execute_tool(app.state.services, "nope", {}, chat_id="t")
    assert "Unknown tool" in result["error"]


async def test_create_agent_tool_pins_model(app):
    install_fake_ai(app, [FakeMessage(content="Designed.")])
    services = app.state.services
    result = await execute_tool(
        services,
        "create_agent",
        {"brief": "A quick helper for tiny lookups.", "model": "beam-free"},
        chat_id="default",
    )
    assert result["ok"] is True, result
    agent = services.team.get_agent(result["result"]["agent_id"])
    assert agent["model"] == "beam-free"


async def test_schedule_task_with_agent_runs_its_model(app):
    fake = install_fake_ai(app, [FakeMessage(content="Scheduled result.")])
    services = app.state.services
    services.prefs.update({"auto_approve_writes": True})
    agent = services.team.create_agent(
        "Nightly",
        "Cheap runner",
        "Do the nightly check.",
        model="hy4-flash-free",
    )
    result = await execute_tool(
        services,
        "schedule_task",
        {
            "title": "Nightly check",
            "prompt": "Check the site",
            "kind": "once",
            "schedule": "2026-12-01T09:30",
            "agent": "Nightly",
        },
        chat_id="default",
    )
    assert result["ok"] is True, result
    assert result["result"]["agent"] == "Nightly"
    task = services.scheduler.get(result["result"]["task_id"])
    assert task["agent_id"] == agent["id"]

    reply = await services.scheduler.runner(task)
    assert reply == "Scheduled result."
    assert fake.models_used == ["hy4-flash-free"]


async def test_schedule_task_accepts_agent_id_alias(app):
    install_fake_ai(app, [FakeMessage(content="unused")])
    services = app.state.services
    services.prefs.update({"auto_approve_writes": True})
    agent = services.team.create_agent("Alias", "Runner", "Do things.")
    result = await execute_tool(
        services,
        "schedule_task",
        {
            "title": "Alias task",
            "prompt": "Do it",
            "kind": "once",
            "schedule": "2026-12-01T09:30",
            "agent_id": agent["id"],
        },
        chat_id="default",
    )
    assert result["ok"] is True, result
    assert result["result"]["agent"] == "Alias"
    task = services.scheduler.get(result["result"]["task_id"])
    assert task["agent_id"] == agent["id"]


async def test_schedule_task_rejects_unknown_agent(app):
    install_fake_ai(app, [FakeMessage(content="unused")])
    services = app.state.services
    services.prefs.update({"auto_approve_writes": True})
    result = await execute_tool(
        services,
        "schedule_task",
        {
            "title": "Broken",
            "prompt": "Do something",
            "kind": "once",
            "schedule": "2026-12-01T09:30",
            "agent": "Nobody",
        },
        chat_id="default",
    )
    assert result.get("ok") is not True
    assert "Unknown agent" in result["error"]
    assert "Active agents" in result["error"]


async def test_list_team_tasks_exposes_error(app):
    services = app.state.services
    team = services.team
    agent = team.create_agent("Err", "Runner", "Do it.")
    task = team.create_task("Broken", "Do it.", agent["id"])
    team.claim_task(task["id"])
    team.fail_task(task["id"], "kaboom")
    result = await execute_tool(
        services, "list_team_tasks", {"limit": 5}, chat_id="default"
    )
    assert result["ok"] is True
    mine = [r for r in result["result"]["tasks"] if r["id"] == task["id"]]
    assert mine
    assert mine[0]["error"] == "kaboom"
    assert mine[0]["result"] == "kaboom"


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
