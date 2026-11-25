"""Scheduled runs must report back into a chat the user can see."""
from __future__ import annotations

from app.tools import execute_tool


async def test_scheduled_result_lands_in_the_main_chat(app):
    services = app.state.services
    assert services.scheduler.notifier is not None
    task = services.scheduler.create("Minute ping", "Say hi", "once", "2026-01-01T00:00")

    await services.scheduler.notifier(task, "ok", "Hello from the scheduler.")

    rows = services.agent.history_for_ui("default", 5)
    assert rows[-1]["role"] == "assistant"
    assert "Hello from the scheduler." in rows[-1]["content"]
    assert "Minute ping" in rows[-1]["content"]
    assert "#%d" % task["id"] in rows[-1]["content"]


async def test_agent_task_result_lands_in_that_agents_dm(app):
    services = app.state.services
    agent = services.team.create_agent("Ada", "Analyst", "Analyse tables.")
    task = services.scheduler.create(
        "Ada ping", "Say hi", "once", "2026-01-01T00:00", agent_id=agent["id"]
    )

    await services.scheduler.notifier(task, "ok", "Ada reporting.")

    rows = services.agent.history_for_ui("agent:%d:dm" % agent["id"], 5)
    assert "Ada reporting." in rows[-1]["content"]
    assert services.agent.history_for_ui("default", 5) == []


async def test_failed_scheduled_result_is_flagged_in_chat(app):
    services = app.state.services
    task = services.scheduler.create("Broken ping", "Say hi", "once", "2026-01-01T00:00")

    await services.scheduler.notifier(task, "error", "Error: boom")

    content = services.agent.history_for_ui("default", 5)[-1]["content"]
    assert "FAILED" in content
    assert "boom" in content

def test_chat_latest_exposes_background_posts(app, auth_client):
    services = app.state.services
    agent = services.team.create_agent("Jennifer", "Assistant", "Check in.")
    services.agent.attach("default", "Lead note.")
    services.agent.attach("agent:%d:dm" % agent["id"], "Jennifer note.")
    services.agent.attach("task:9", "Private run transcript.")

    rows = auth_client.get("/api/chat/latest").json()["chats"]
    by_chat = {row["chat_id"]: row for row in rows}
    assert by_chat["default"]["role"] == "assistant"
    assert by_chat["agent:%d:dm" % agent["id"]]["role"] == "assistant"
    assert "task:9" not in by_chat
    assert all(row["id"] for row in rows)


async def test_scheduled_agent_task_runs_under_the_agent_identity(app, monkeypatch):
    services = app.state.services
    agent = services.team.create_agent("Jennifer", "Assistant", "Check in.")
    task = services.scheduler.create(
        "Check-in", "Say hi", "once", "2026-01-01T00:00", agent_id=agent["id"]
    )
    seen = {}

    class FakeAgent:
        def __init__(self, services_arg, profile=None):
            seen["profile"] = profile

        async def run(self, chat_id, user_text, actor):
            seen["chat_id"] = chat_id
            return {"reply": "hi"}

    monkeypatch.setattr("app.main.Agent", FakeAgent)
    out = await services.scheduler.runner(task)

    assert out == "hi"
    assert seen["chat_id"] == "agent:%d:task:%d" % (agent["id"], task["id"])
    assert seen["profile"]["name"] == "Jennifer"


async def test_muse_scheduled_task_keeps_the_lead_chat(app, monkeypatch):
    services = app.state.services
    task = services.scheduler.create("Ping", "Say hi", "once", "2026-01-01T00:00")
    seen = {}

    async def fake_run(chat_id, user_text, actor):
        seen["chat_id"] = chat_id
        return {"reply": "ok"}

    monkeypatch.setattr(services.agent, "run", fake_run)
    await services.scheduler.runner(task)

    assert seen["chat_id"] == "task:%d" % task["id"]


async def test_agent_scheduled_message_keeps_its_sender(app):
    services = app.state.services
    agent = services.team.create_agent("Jennifer", "Assistant", "Check in.")
    out = await execute_tool(
        services,
        "send_message",
        {"agent": "lead", "content": "Check-in: all good."},
        chat_id="agent:%d:task:4" % agent["id"],
    )
    assert "error" not in out, out
    row = services.db.query(
        "SELECT from_agent_id, to_agent_id, content FROM agent_messages "
        "ORDER BY id DESC LIMIT 1"
    )[0]
    assert row["from_agent_id"] == agent["id"]
    assert row["to_agent_id"] is None

