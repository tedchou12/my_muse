"""Scheduled runs must report back into a chat the user can see."""
from __future__ import annotations


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
