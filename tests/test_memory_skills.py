from __future__ import annotations

import sqlite3

from app.agent import Agent
from app.db import Database
from app.scope import LEAD_SCOPE, scope_from_chat, scope_from_profile, scope_from_source
from app.tools import execute_tool
from conftest import CSRF


async def _run(services, name, args, chat_id):
    out = await execute_tool(services, name, args, chat_id=chat_id)
    assert "error" not in out, out
    return out["result"]


async def _err(services, name, args, chat_id):
    out = await execute_tool(services, name, args, chat_id=chat_id)
    assert "error" in out, out
    return out["error"]


def test_scope_helpers_are_strict():
    assert scope_from_chat("default") == LEAD_SCOPE
    assert scope_from_chat("task:9") == LEAD_SCOPE
    assert scope_from_chat("agent:7:dm") == "agent:7"
    assert scope_from_chat("agent:7:task:3") == "agent:7"
    assert scope_from_source("agent:12:task:2") == "agent:12"
    assert scope_from_source("default") == LEAD_SCOPE
    assert scope_from_profile({}) == LEAD_SCOPE
    assert scope_from_profile({"id": 0}) == LEAD_SCOPE
    assert scope_from_profile({"id": 5}) == "agent:5"


def test_legacy_memories_are_backfilled_per_owner(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE memories ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "content TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT '', "
        "created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO memories (content, source, created_at) "
        "VALUES ('agent fact', 'agent:4:dm', 1.0)"
    )
    conn.execute(
        "INSERT INTO memories (content, source, created_at) "
        "VALUES ('lead fact', 'default', 2.0)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        rows = {
            row["content"]: row["scope"]
            for row in db.query("SELECT content, scope FROM memories")
        }
    finally:
        db.close()
    assert rows == {"agent fact": "agent:4", "lead fact": LEAD_SCOPE}


async def test_memory_is_private_per_agent(app):
    services = app.state.services
    await _run(services, "remember", {"fact": "Ada tracks tables."}, "agent:1:dm")
    await _run(services, "remember", {"fact": "The lead tracks charts."}, "default")

    assert (await _run(services, "recall", {}, "agent:1:dm"))["memories"] == [
        "Ada tracks tables."
    ]
    assert (await _run(services, "recall", {}, "agent:2:dm"))["memories"] == []
    assert (await _run(services, "recall", {}, "default"))["memories"] == [
        "The lead tracks charts."
    ]


async def test_lead_can_read_an_agent_memory_but_agents_cannot(app):
    services = app.state.services
    agent = services.team.create_agent("Ada", "Analyst", "Analyse tables.")
    chat_id = "agent:%d:dm" % agent["id"]
    await _run(
        services,
        "remember",
        {"fact": "Quarterly numbers live in /data/q.xlsx."},
        chat_id,
    )

    lead_view = await _run(services, "recall", {"agent": "Ada"}, "default")
    assert lead_view["scope"] == "agent:%d" % agent["id"]
    assert "q.xlsx" in lead_view["memories"][0]

    error = await _err(services, "recall", {"agent": "Ada"}, chat_id)
    assert "private" in error

async def test_skills_are_private_per_owner(app):
    services = app.state.services
    await _run(
        services,
        "skill_save",
        {
            "name": "weekly-report",
            "summary": "Build the weekly report",
            "instructions": "1. Pull data\n2. Chart it",
        },
        "agent:1:dm",
    )

    loaded = await _run(
        services, "skill_use", {"name": "Weekly-Report"}, "agent:1:dm"
    )
    assert loaded["instructions"].startswith("1. Pull data")
    assert loaded["summary"] == "Build the weekly report"

    listed = await _run(services, "skill_list", {}, "agent:1:dm")
    assert [item["name"] for item in listed["skills"]] == ["weekly-report"]

    error = await _err(
        services, "skill_use", {"name": "weekly-report"}, "agent:2:dm"
    )
    assert "No skill named" in error

    assert (await _run(services, "skill_list", {}, "default"))["skills"] == []


async def test_lead_skill_inspection_is_read_only(app):
    services = app.state.services
    agent = services.team.create_agent("Ada", "Analyst", "Analyse tables.")
    chat_id = "agent:%d:dm" % agent["id"]
    await _run(
        services,
        "skill_save",
        {"name": "audit", "instructions": "Open the ledger, then reconcile."},
        chat_id,
    )

    viewed = await _run(
        services, "skill_use", {"name": "audit", "agent": "Ada"}, "default"
    )
    assert viewed["instructions"].startswith("Open the ledger")

    error = await _err(
        services, "skill_delete", {"name": "audit", "agent": "Ada"}, "default"
    )
    assert "owner" in error
    assert (await _run(services, "skill_list", {}, chat_id))["skills"]


def test_prompt_shows_only_own_memory_and_skill_summaries(app):
    services = app.state.services
    services.memories.remember("agent:1", "Ada keeps the raw CSV.")
    services.memories.remember(LEAD_SCOPE, "The lead prefers ET timestamps.")
    services.skills.save(
        "agent:1", "csv-clean", "Step one: drop the header row.", "Clean the CSV"
    )

    agent_prompt = Agent(
        services, profile={"id": 1, "name": "Ada", "role": "Analyst"}
    ).system_prompt()
    assert "Ada keeps the raw CSV." in agent_prompt
    assert "The lead prefers ET timestamps." not in agent_prompt
    assert "csv-clean: Clean the CSV" in agent_prompt
    assert "Step one: drop the header row." not in agent_prompt

    lead_prompt = Agent(services).system_prompt()
    assert "The lead prefers ET timestamps." in lead_prompt
    assert "Ada keeps the raw CSV." not in lead_prompt
    assert "csv-clean" not in lead_prompt


def test_team_api_lists_and_edits_private_scopes(app, auth_client):
    services = app.state.services
    agent = services.team.create_agent("Ada", "Analyst", "Analyse tables.")
    agent_id = str(agent["id"])
    services.memories.remember("agent:%s" % agent_id, "Ada noted a thing.")
    services.memories.remember(LEAD_SCOPE, "Lead noted a thing.")

    listing = auth_client.get("/api/team").json()
    assert listing["lead"]["memory_count"] == 1
    assert listing["agents"][0]["memory_count"] == 1

    memory = auth_client.get("/api/team/memory?agent=" + agent_id).json()
    assert memory["scope"] == "agent:" + agent_id
    assert [item["content"] for item in memory["memories"]] == ["Ada noted a thing."]

    lead_memory = auth_client.get("/api/team/memory?agent=lead").json()
    assert [item["content"] for item in lead_memory["memories"]] == [
        "Lead noted a thing."
    ]

    created = auth_client.post(
        "/api/team/skills",
        json={
            "agent": agent_id,
            "name": "reconcile",
            "summary": "Reconcile the ledger",
            "instructions": "Open the ledger and reconcile it.",
        },
        headers=CSRF,
    )
    assert created.status_code == 200, created.text
    skill = created.json()["skill"]
    assert skill["scope"] == "agent:" + agent_id

    fetched = auth_client.get("/api/team/skills/%d" % skill["id"]).json()["skill"]
    assert fetched["instructions"].startswith("Open the ledger")

    updated = auth_client.put(
        "/api/team/skills/%d" % skill["id"],
        json={"summary": "Reconcile nightly"},
        headers=CSRF,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["skill"]["summary"] == "Reconcile nightly"

    assert auth_client.get("/api/team/skills?agent=lead").json()["skills"] == []
    assert auth_client.get("/api/team/skills?agent=999").status_code == 404

    assert (
        auth_client.delete(
            "/api/team/skills/%d" % skill["id"], headers=CSRF
        ).status_code
        == 200
    )
    assert (
        auth_client.delete(
            "/api/team/memory/%d" % memory["memories"][0]["id"], headers=CSRF
        ).status_code
        == 200
    )
    assert auth_client.get("/api/team/memory?agent=" + agent_id).json()["memories"] == []

