from __future__ import annotations

import json

import pytest

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
