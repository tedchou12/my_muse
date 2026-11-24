from __future__ import annotations

import asyncio
import json

from conftest import (
    CSRF,
    FakeMessage,
    FakeOpenAI,
    FakeResponse,
    FakeToolCall,
    install_fake_ai,
)


def _create_agent(team, name, role, instructions):
    return team.create_agent(name, role, instructions, created_by="user")


def test_create_agent_from_prompt_and_list(app, auth_client):
    response = auth_client.post(
        "/api/team/agents",
        json={
            "brief": (
                "A meticulous researcher who maps official government pages, "
                "cites exact URLs and never invents facts."
            ),
            "name": "Researcher",
        },
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    agent = response.json()["agent"]
    assert agent["name"] == "Researcher"
    assert agent["role"]
    assert agent["status"] == "active"

    listing = auth_client.get("/api/team").json()
    assert [item["name"] for item in listing["agents"]] == ["Researcher"]
    assert listing["limits"]["max_depth"] >= 1
    agents_only = auth_client.get("/api/team/agents").json()
    assert [item["name"] for item in agents_only["agents"]] == ["Researcher"]


def test_team_task_parent_depth_and_result_message(app):
    team = app.state.services.team
    lead = _create_agent(team, "Lead", "Coordinates the work.", "Delegate clearly.")
    researcher = _create_agent(
        team, "Researcher", "Finds primary sources.", "Cite every source."
    )
    parent = team.create_task("Research", "Find the facts.", lead["id"])
    child = team.create_task(
        "Find sources",
        "Find two primary sources.",
        researcher["id"],
        created_by_agent_id=lead["id"],
        parent_task_id=parent["id"],
    )
    assert child["depth"] == 1
    assert child["parent_task_id"] == parent["id"]
    assert child["created_by_agent_id"] == lead["id"]

    inbox = team.inbox(researcher["id"])
    assert inbox and "Find two primary sources" in inbox[-1]["content"]

    claimed = team.claim_task(child["id"])
    assert claimed["status"] == "working"
    done = team.complete_task(child["id"], "Two sources found.")
    assert done["status"] == "done"
    assert done["result"] == "Two sources found."
    lead_inbox = team.inbox(lead["id"])
    assert any(
        item["kind"] == "result" and "Two sources found" in item["content"]
        for item in lead_inbox
    )


def test_depth_limit_blocks_runaway_delegation(app):
    team = app.state.services.team
    lead = _create_agent(team, "Lead", "Leads.", "Lead.")
    worker = _create_agent(team, "Worker", "Works.", "Work.")
    parent = team.create_task("Root", "Root task.", lead["id"])
    task = parent
    for index in range(app.state.services.settings.team_max_depth):
        task = team.create_task(
            "Layer %d" % index,
            "Nested task.",
            worker["id"],
            created_by_agent_id=lead["id"],
            parent_task_id=task["id"],
        )
    try:
        team.create_task(
            "Too deep",
            "Should fail.",
            worker["id"],
            created_by_agent_id=lead["id"],
            parent_task_id=task["id"],
        )
    except Exception as exc:
        assert "depth limit" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("depth limit was not enforced")


def test_request_vault_entry_can_be_fulfilled_without_browser(app, auth_client):
    from app.tools import execute_tool

    services = app.state.services
    result = asyncio.run(
        execute_tool(
            services,
            "request_vault_entry",
            {
                "domain": "google.com",
                "label": "Google",
                "reason": "Read a verification email.",
            },
            "vault-card-chat",
        )
    )
    assert result["ok"] is True
    request = result["result"]
    assert request["mode"] == "vault"
    pending = services.credentials.get(request["request_id"])
    assert pending["login_url"] == ""

    services.vault.initialize("test-master-password")
    fulfilled = auth_client.post(
        "/api/credential-requests/%d/fulfill" % request["request_id"],
        json={
            "label": "Google",
            "username": "user@example.com",
            "password": "not-a-real-password",
            "notes": "test",
        },
        headers=CSRF,
    )
    assert fulfilled.status_code == 200, fulfilled.text
    assert fulfilled.json()["login_ok"] is False
    entry = services.vault.get_secret_by_label("Google")
    assert entry["domains"] == ["google.com"]
    assert services.credentials.get(request["request_id"])["status"] == "fulfilled"


def test_rename_self_tool_only_updates_calling_agent(app):
    from app.tools import execute_tool

    services = app.state.services
    agent = _create_agent(
        services.team,
        "Travel Desk",
        "Handles travel.",
        "Keep the travel brief unchanged.",
    )
    result = asyncio.run(
        execute_tool(
            services,
            "rename_self",
            {"name": "Assistant", "role": "General helper"},
            "agent:%d" % agent["id"],
        )
    )
    assert result["ok"] is True
    assert result["result"]["name"] == "Assistant"
    updated = services.team.get_agent(agent["id"])
    assert updated["name"] == "Assistant"
    assert updated["role"] == "General helper"
    assert updated["instructions"] == "Keep the travel brief unchanged."

    denied = asyncio.run(
        execute_tool(
            services,
            "rename_self",
            {"name": "Other"},
            "default",
        )
    )
    assert "error" in denied
    assert services.team.get_agent(agent["id"])["name"] == "Assistant"


def test_assign_task_tool_uses_agent_context(app):
    from app.tools import execute_tool

    services = app.state.services
    team = services.team
    lead = _create_agent(team, "Lead", "Leads.", "Lead.")
    worker = _create_agent(team, "Worker", "Works.", "Work.")
    parent = team.create_task("Root", "Root task.", lead["id"])
    result = asyncio.run(
        execute_tool(
            services,
            "assign_task",
            {
                "agent": "Worker",
                "title": "Sub-task",
                "prompt": "Do the sub-task.",
            },
            chat_id="agent:%d:task:%d" % (lead["id"], parent["id"]),
            actor="agent:%d" % lead["id"],
        )
    )
    assert result["ok"] is True
    task = team.get_task(result["result"]["task_id"])
    assert task["created_by_agent_id"] == lead["id"]
    assert task["parent_task_id"] == parent["id"]
    assert task["depth"] == 1


def test_create_agent_tool_is_internal(app):
    from app.tools import execute_tool

    services = app.state.services
    result = asyncio.run(
        execute_tool(
            services,
            "create_agent",
            {
                "brief": "An editor who turns notes into a clean brief.",
                "name": "Editor",
            },
            chat_id="default",
            actor="agent",
        )
    )
    assert result["ok"] is True
    assert result["result"]["name"] == "Editor"
    assert services.team.get_agent(name="Editor")["status"] == "active"


def test_team_runner_executes_assigned_agent(app):
    fake = install_fake_ai(app, [FakeMessage(content="Research complete.")])
    services = app.state.services
    team = services.team
    agent = _create_agent(
        team, "Researcher", "Finds primary sources.", "Return the findings."
    )
    task = team.create_task("Research", "Find the facts.", agent["id"])
    result = asyncio.run(services.team_runner.run_task(task["id"]))
    assert result["status"] == "done"
    assert result["result"] == "Research complete."
    assert result["run_count"] == 1
    assert fake.chat_calls
    assert "Research" in fake.chat_calls[0][1]["content"]


def test_agent_group_conversation_and_user_message(app):
    team = app.state.services.team
    first = _create_agent(team, "Alpha", "First agent.", "First.")
    second = _create_agent(team, "Beta", "Second agent.", "Second.")
    team.send_message(
        first["id"], second["id"], "I found the primary source."
    )
    groups = team.list_groups()
    assert groups and groups[0]["title"] == "Alpha ↔ Beta"
    assert groups[0]["last_message"] == "I found the primary source."
    messages = team.group_messages(first["id"], second["id"])
    assert [item["content"] for item in messages] == [
        "I found the primary source."
    ]

    team.send_group_message(first["id"], second["id"], "Please compare notes.")
    messages = team.group_messages(first["id"], second["id"])
    assert [item["content"] for item in messages] == [
        "I found the primary source.",
        "Please compare notes.",
    ]
    for agent in (first, second):
        inbox = team.inbox(agent["id"])
        assert any(item["content"] == "Please compare notes." for item in inbox)


def test_muse_agent_group_shows_assignment_and_result(app):
    team = app.state.services.team
    agent = _create_agent(team, "Sourcing Scout", "Finds products.", "Source well.")
    task = team.create_task(
        "Find a budget adapter", "Find one under ¥500.", agent["id"]
    )
    team.claim_task(task["id"])
    team.complete_task(task["id"], "Found two options under ¥500.")
    groups = team.list_groups()
    muse_group = next(
        item for item in groups if item["title"] == "Muse ↔ Sourcing Scout"
    )
    assert muse_group["agent_a_id"] == 0
    messages = team.group_messages(0, agent["id"])
    contents = [item["content"] for item in messages]
    assert any("Find a budget adapter" in item for item in contents)
    assert "Found two options under ¥500." in contents
    assert any(item["kind"] == "task_result" for item in messages)


def test_team_api_group_conversation(app, auth_client):
    team = app.state.services.team
    first = _create_agent(team, "Alpha", "First agent.", "First.")
    second = _create_agent(team, "Beta", "Second agent.", "Second.")
    team.send_message(first["id"], second["id"], "Agent to agent.")
    groups = auth_client.get("/api/team/groups").json()["groups"]
    assert len(groups) == 1
    assert groups[0]["title"] == "Alpha ↔ Beta"

    detail = auth_client.get(
        "/api/team/groups/%d/%d" % (second["id"], first["id"])
    ).json()
    assert detail["group"]["agent_a_id"] == first["id"]
    assert detail["messages"][0]["content"] == "Agent to agent."

    sent = auth_client.post(
        "/api/team/groups/%d/%d/messages" % (first["id"], second["id"]),
        json={"content": "Hello both."},
        headers=CSRF,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["message"]["content"] == "Hello both."


def test_chat_cancel_endpoint(app, auth_client):
    services = app.state.services
    event = services.cancellations.begin("cancel-test")
    response = auth_client.post(
        "/api/chat/cancel",
        json={"chat_id": "cancel-test"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json()["cancelled"] is True
    assert event.is_set()
    services.cancellations.finish("cancel-test", event)


def test_cancelling_a_running_agent_stops_the_loop(app):
    services = app.state.services
    services.settings.max_agent_steps = 0

    class SlowLoop(FakeOpenAI):
        async def achat(
            self, messages, tools=None, temperature=None, session_id=None, model=None
        ):
            self.models_used.append(model)
            await asyncio.sleep(0.02)
            return FakeResponse(
                FakeMessage(
                    tool_calls=[FakeToolCall("x", "recall", {"query": "loop"})]
                )
            )

    services.openai = SlowLoop(services.settings)

    async def scenario():
        running = asyncio.create_task(
            services.agent.run(chat_id="cancel-run", user_text="Keep going")
        )
        await asyncio.sleep(0.06)
        assert services.cancellations.cancel("cancel-run") is True
        return await running

    result = asyncio.run(scenario())
    assert result["reply"].startswith("Stopped at your request.")
    assert services.cancellations.is_cancelled("cancel-run") is False


def test_chat_queues_message_while_run_active(app, auth_client):
    services = app.state.services
    assert services.chat_queue.try_begin("queue-chat") is True
    try:
        response = auth_client.post(
            "/api/chat",
            json={"message": "follow up", "chat_id": "queue-chat"},
            headers=CSRF,
        )
        assert response.status_code == 200, response.text
        assert response.json()["queued"] is True
        assert services.chat_queue.drain("queue-chat") == ["follow up"]
    finally:
        services.chat_queue.finish("queue-chat")
    history = auth_client.get("/api/chat/history?chat_id=queue-chat").json()
    assert [message["content"] for message in history["messages"]] == ["follow up"]


def test_queued_message_enters_agent_context(app):
    services = app.state.services
    services.settings.max_agent_steps = 0

    class TwoStep(FakeOpenAI):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls = 0

        async def achat(
            self, messages, tools=None, temperature=None, session_id=None, model=None
        ):
            self.chat_calls.append(messages)
            self.models_used.append(model)
            self.calls += 1
            await asyncio.sleep(0.02)
            if self.calls == 1:
                return FakeResponse(
                    FakeMessage(
                        tool_calls=[FakeToolCall("1", "recall", {"query": "seed"})]
                    )
                )
            return FakeResponse(FakeMessage(content="Follow-up received."))

    fake = TwoStep(services.settings)
    services.openai = fake
    assert services.chat_queue.try_begin("queue-context") is True

    async def scenario():
        running = asyncio.create_task(
            services.agent.run(chat_id="queue-context", user_text="Start")
        )
        await asyncio.sleep(0.01)
        assert services.chat_queue.enqueue("queue-context", "follow up") is True
        return await running

    try:
        result = asyncio.run(scenario())
    finally:
        services.chat_queue.finish("queue-context")
    assert result["reply"] == "Follow-up received."
    assert any(
        message.get("role") == "user" and message.get("content") == "follow up"
        for message in fake.chat_calls[-1]
    )


def test_direct_chat_with_named_agent(app, auth_client):
    fake = install_fake_ai(app, [FakeMessage(content="Direct DM reply.")])
    agent = app.state.services.team.create_agent(
        "Scout", "Finds primary sources.", "Cite sources."
    )
    response = auth_client.post(
        "/api/chat",
        json={
            "message": "Hello Scout",
            "chat_id": "agent:%d:dm" % agent["id"],
            "agent_id": agent["id"],
        },
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "Direct DM reply."
    system_prompt = fake.chat_calls[0][0]["content"]
    assert "Scout" in system_prompt
    assert "Finds primary sources" in system_prompt
    history = auth_client.get(
        "/api/chat/history?chat_id=agent:%d:dm" % agent["id"]
    ).json()
    assert [message["role"] for message in history["messages"]] == [
        "user",
        "assistant",
    ]


def test_team_api_task_flow(app, auth_client):
    fake = install_fake_ai(app, [FakeMessage(content="API task done.")])
    created = auth_client.post(
        "/api/team/agents",
        json={"brief": "A writer who turns research into a short brief.", "name": "Writer"},
        headers=CSRF,
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["agent"]["id"]
    task = auth_client.post(
        "/api/team/tasks",
        json={
            "agent_id": agent_id,
            "title": "Write brief",
            "prompt": "Write a three-bullet brief.",
        },
        headers=CSRF,
    )
    assert task.status_code == 200, task.text
    task_id = task.json()["task"]["id"]

    run = auth_client.post("/api/team/tasks/%d/run" % task_id, headers=CSRF)
    assert run.status_code == 200, run.text
    assert run.json()["task"]["status"] == "done"
    assert fake.chat_calls

    detail = auth_client.get("/api/team/tasks/%d" % task_id).json()
    assert detail["task"]["result"] == "API task done."
    assert any(item["kind"] == "assignment" for item in detail["messages"])


def test_agent_trusted_commands_persist(app, auth_client):
    created = auth_client.post(
        "/api/team/agents",
        json={
            "brief": "A tiny kiosk bot that only checks a status endpoint.",
            "name": "Tiny",
            "trusted_commands": ["git status", "curl -s https://api.example.com*"],
        },
        headers=CSRF,
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    assert json.loads(agent["trusted_commands"]) == [
        "git status",
        "curl -s https://api.example.com*",
    ]

    updated = auth_client.patch(
        "/api/team/agents/%d" % agent["id"],
        json={"trusted_commands": ["ls"]},
        headers=CSRF,
    )
    assert updated.status_code == 200, updated.text
    assert json.loads(updated.json()["agent"]["trusted_commands"]) == ["ls"]


def test_agent_model_created_via_api_and_persisted(app, auth_client):
    created = auth_client.post(
        "/api/team/agents",
        json={
            "brief": "A lightweight helper for small lookup jobs.",
            "name": "Cheapskate",
            "model": "hy4-flash-free",
        },
        headers=CSRF,
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    assert agent["model"] == "hy4-flash-free"

    updated = auth_client.patch(
        "/api/team/agents/%d" % agent["id"],
        json={"model": "qwen3.8-flash"},
        headers=CSRF,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["agent"]["model"] == "qwen3.8-flash"

    cleared = auth_client.patch(
        "/api/team/agents/%d" % agent["id"],
        json={"model": ""},
        headers=CSRF,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["agent"]["model"] == ""


def test_chat_with_agent_uses_pinned_model(app, auth_client):
    fake = install_fake_ai(app, [FakeMessage(content="Cheap hello.")])
    services = app.state.services
    agent = services.team.create_agent(
        "Frugal",
        "Cheap assistant",
        "Answer quickly and cheaply.",
        model="hy4-flash-free",
    )
    response = auth_client.post(
        "/api/chat",
        json={
            "message": "hello",
            "chat_id": "agent:%d:dm" % agent["id"],
            "agent_id": agent["id"],
        },
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert fake.models_used == ["hy4-flash-free"]


def test_default_agent_uses_provider_default_model(app):
    fake = install_fake_ai(app, [FakeMessage(content="Hi.")])
    services = app.state.services
    asyncio.run(services.agent.run(chat_id="model-default", user_text="hello"))
    assert fake.models_used == [None]


def test_team_runner_uses_agent_model(app):
    fake = install_fake_ai(app, [FakeMessage(content="Cheap task done.")])
    services = app.state.services
    team = services.team
    agent = team.create_agent(
        "Economist",
        "Cheap runner",
        "Do the job cheaply.",
        model="hy4-flash-free",
    )
    task = team.create_task("Cheap job", "Do it cheaply.", agent["id"])
    result = asyncio.run(services.team_runner.run_task(task["id"]))
    assert result["status"] == "done"
    assert result["result"] == "Cheap task done."
    assert fake.models_used == ["hy4-flash-free"]


def test_fail_task_fills_empty_result(app):
    team = app.state.services.team
    agent = team.create_agent("Failing", "Runner", "Do it.")
    task = team.create_task("Will fail", "Do it.", agent["id"])
    team.claim_task(task["id"])
    failed = team.fail_task(task["id"], "boom")
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"
    # The board must never show a failed task with a blank result.
    assert failed["result"] == "boom"
