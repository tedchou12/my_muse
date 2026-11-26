from __future__ import annotations

import time

from conftest import (
    CSRF,
    PASSWORD,
    FakeMessage,
    FakeToolCall,
    install_fake_ai,
)
from app.main import build_security_headers

TASK = {
    "title": "Test task",
    "prompt": "Say hello",
    "kind": "once",
    "schedule": "2030-01-01T09:00",
    "timezone": "UTC",
}


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_requires_auth(client):
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/vault/status").status_code == 401
    assert client.get("/api/session").json()["authenticated"] is False


def test_login_and_logout(client):
    bad = client.post("/api/login", json={"password": "wrong-password"})
    assert bad.status_code == 401
    ok = client.post("/api/login", json={"password": PASSWORD})
    assert ok.status_code == 200
    session = client.get("/api/session").json()
    assert session["authenticated"] is True
    assert session["openai_configured"] is True
    assert client.post("/api/logout", headers=CSRF).status_code == 200
    assert client.get("/api/session").json()["authenticated"] is False


def test_csrf_header_required(auth_client):
    response = auth_client.post("/api/tasks", json=TASK)
    assert response.status_code == 400
    assert "X-Muse-Request" in response.json()["detail"]


def test_task_crud(auth_client):
    created = auth_client.post("/api/tasks", json=TASK, headers=CSRF)
    assert created.status_code == 200, created.text
    task = created.json()["task"]
    assert task["next_run_at"] > time.time()

    bad = auth_client.post(
        "/api/tasks",
        json=dict(TASK, schedule="not-a-date"),
        headers=CSRF,
    )
    assert bad.status_code == 400

    listed = auth_client.get("/api/tasks").json()["tasks"]
    assert [item["id"] for item in listed] == [task["id"]]

    paused = auth_client.post("/api/tasks/%d/pause" % task["id"], headers=CSRF)
    assert paused.json()["task"]["active"] == 0
    resumed = auth_client.post("/api/tasks/%d/resume" % task["id"], headers=CSRF)
    assert resumed.json()["task"]["active"] == 1

    runs = auth_client.get("/api/tasks/%d/runs" % task["id"]).json()
    assert runs["runs"] == []

    assert auth_client.delete("/api/tasks/%d" % task["id"], headers=CSRF).status_code == 200
    assert auth_client.get("/api/tasks").json()["tasks"] == []


def test_vault_api_flow(auth_client):
    status = auth_client.get("/api/vault/status").json()
    assert status["initialized"] is False

    created = auth_client.post(
        "/api/vault/initialize",
        json={"master_password": "a very good master phrase"},
        headers=CSRF,
    )
    assert created.status_code == 200
    assert created.json()["unlocked"] is True

    entry = auth_client.post(
        "/api/vault/entries",
        json={
            "label": "GitHub",
            "domains": ["github.com"],
            "username": "octocat",
            "password": "hunter2-hunter2",
            "url": "https://github.com/login",
            "notes": "",
        },
        headers=CSRF,
    )
    assert entry.status_code == 200, entry.text
    entry_id = entry.json()["entry"]["id"]

    listed = auth_client.get("/api/vault/entries").json()
    assert listed["entries"][0]["label"] == "GitHub"
    assert "password" not in listed["entries"][0]

    revealed = auth_client.post(
        "/api/vault/entries/%d/reveal" % entry_id, headers=CSRF
    )
    assert revealed.json()["secret"]["password"] == "hunter2-hunter2"

    auth_client.post("/api/vault/lock", headers=CSRF)
    locked = auth_client.post(
        "/api/vault/entries/%d/reveal" % entry_id, headers=CSRF
    )
    assert locked.status_code == 423

    wrong = auth_client.post(
        "/api/vault/unlock", json={"master_password": "nope-nope"}, headers=CSRF
    )
    assert wrong.status_code == 403
    right = auth_client.post(
        "/api/vault/unlock",
        json={"master_password": "a very good master phrase"},
        headers=CSRF,
    )
    assert right.status_code == 200
    assert right.json()["unlocked"] is True


def test_chat_tool_loop(app, auth_client):
    fake = install_fake_ai(
        app,
        [
            FakeMessage(tool_calls=[FakeToolCall("1", "list_tasks", {})]),
            "Nothing is scheduled.",
        ],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "What is scheduled?", "chat_id": "c1"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reply"] == "Nothing is scheduled."
    assert data["steps"][0]["tool"] == "list_tasks"
    assert data["steps"][0]["ok"] is True
    assert len(fake.chat_calls) == 2

    history = auth_client.get("/api/chat/history?chat_id=c1").json()
    assert [message["role"] for message in history["messages"]] == [
        "user",
        "assistant",
    ]
    activity = auth_client.get("/api/activity").json()
    assert any(row["action"] == "tool_list_tasks" for row in activity["audit"])


def test_pending_approval_stops_the_loop(app, auth_client):
    app.state.services.prefs.update({"auto_approve_browser": False})
    fake = install_fake_ai(
        app,
        [
            FakeMessage(
                tool_calls=[FakeToolCall("1", "browser_click", {"target": "#buy"})]
            ),
            "This follow-up should not be requested.",
        ],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Buy it", "chat_id": "pending-stop"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(fake.chat_calls) == 1
    assert len(data["pending_approvals"]) == 1
    assert "Approval #" in data["reply"]
    assert "continue automatically" in data["reply"]


def test_repeated_tool_call_recovers_and_returns_final_reply(app, auth_client):
    app.state.services.settings.max_agent_steps = 0
    fake = install_fake_ai(
        app,
        [
            FakeMessage(tool_calls=[FakeToolCall(str(i), "list_tasks", {})])
            for i in range(4)
        ]
        + ["I changed approach and finished."],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Keep trying", "chat_id": "repeat-stop"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(fake.chat_calls) == 5
    assert data["reply"] == "I changed approach and finished."
    assert "kept repeating" not in data["reply"]
    executed = [step for step in data["steps"] if step["tool"] == "list_tasks"]
    assert len(executed) == 4
    assert sum(1 for step in executed if step["ok"]) == 3
    assert "already been tried three times" in executed[-1]["error"]


def test_repeated_tool_call_stops_without_the_old_diagnostic(app, auth_client):
    app.state.services.settings.max_agent_steps = 0
    fake = install_fake_ai(
        app,
        [
            FakeMessage(tool_calls=[FakeToolCall(str(i), "list_tasks", {})])
            for i in range(6)
        ]
        + ["This follow-up should not be requested."],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Keep trying", "chat_id": "repeat-hard-stop"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(fake.chat_calls) == 6
    assert "kept repeating" not in data["reply"]
    assert "fresh budget" not in data["reply"]
    assert "I couldn't complete that step" in data["reply"]


def test_step_limit_reply_summarises_recent_actions(app, auth_client):
    fake = install_fake_ai(
        app,
        [
            FakeMessage(tool_calls=[FakeToolCall("1", "recall", {"query": "one"})]),
            FakeMessage(tool_calls=[FakeToolCall("2", "recall", {"query": "two"})]),
            FakeMessage(tool_calls=[FakeToolCall("3", "recall", {"query": "three"})]),
            FakeMessage(tool_calls=[FakeToolCall("4", "recall", {"query": "four"})]),
        ],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Long research task", "chat_id": "limit-stop"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(fake.chat_calls) == 4
    assert "I used all 4 tool rounds" in data["reply"]
    assert "recall ok" in data["reply"]
    assert "Reply 'continue'" in data["reply"]


def test_no_step_limit_when_disabled(app, auth_client):
    app.state.services.settings.max_agent_steps = 0
    fake = install_fake_ai(
        app,
        [
            FakeMessage(
                tool_calls=[FakeToolCall(str(i), "recall", {"query": "q%d" % i})]
            )
            for i in range(6)
        ]
        + ["Finished after six rounds."],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Keep working", "chat_id": "unlimited"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reply"] == "Finished after six rounds."
    assert len(fake.chat_calls) == 7
    assert len(data["steps"]) == 6


def test_step_budget_nudge_is_sent_near_the_limit(app, auth_client):
    fake = install_fake_ai(
        app,
        [
            FakeMessage(tool_calls=[FakeToolCall("1", "recall", {"query": "one"})]),
            FakeMessage(tool_calls=[FakeToolCall("2", "recall", {"query": "two"})]),
            "Final answer.",
        ],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Research this", "chat_id": "nudge"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "Final answer."
    assert len(fake.chat_calls) == 3
    assert any(
        message.get("role") == "system"
        and "Step limit: only 2 model round(s) remain" in message.get("content", "")
        for message in fake.chat_calls[2]
    )


def test_approval_flow_generates_image(app, auth_client):
    fake = install_fake_ai(
        app,
        [
            FakeMessage(
                tool_calls=[
                    FakeToolCall("1", "generate_image", {"prompt": "a lighthouse"})
                ]
            ),
            "Approved and continued.",
        ],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Draw a lighthouse", "chat_id": "art"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    pending = response.json()["pending_approvals"]
    assert len(pending) == 1
    approval_id = pending[0]["approval_id"]

    inbox = auth_client.get("/api/approvals").json()
    assert [item["id"] for item in inbox["pending"]] == [approval_id]

    approved = auth_client.post(
        "/api/approvals/%d/approve" % approval_id, headers=CSRF
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["status"] == "approved"
    assert approved.json()["followup"]["reply"] == "Approved and continued."
    assert len(fake.chat_calls) == 2

    artifact = app.state.services.db.one(
        "SELECT * FROM artifacts ORDER BY id DESC LIMIT 1"
    )
    assert artifact["kind"] == "image"
    raw = auth_client.get("/api/artifacts/%d/raw" % artifact["id"])
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("image/png")

    again = auth_client.post(
        "/api/approvals/%d/approve" % approval_id, headers=CSRF
    )
    assert again.status_code == 409


def test_approval_rejection(app, auth_client):
    app.state.services.prefs.update({"auto_approve_browser": False})
    install_fake_ai(
        app,
        [
            FakeMessage(
                tool_calls=[FakeToolCall("1", "browser_click", {"target": "#buy"})]
            ),
            "I need approval before clicking that button.",
        ],
    )
    response = auth_client.post(
        "/api/chat",
        json={"message": "Buy it", "chat_id": "reject"},
        headers=CSRF,
    )
    approval_id = response.json()["pending_approvals"][0]["approval_id"]

    rejected = auth_client.post(
        "/api/approvals/%d/reject" % approval_id,
        json={"reason": "Too risky"},
        headers=CSRF,
    )
    assert rejected.status_code == 200
    assert rejected.json()["approval"]["status"] == "rejected"
    assert rejected.json()["approval"]["result"] == "Too risky"


def test_settings_validation_and_persistence(auth_client):
    invalid = auth_client.put(
        "/api/settings", json={"timezone": "Mars/Olympus"}, headers=CSRF
    )
    assert invalid.status_code == 400

    updated = auth_client.put(
        "/api/settings",
        json={
            "agent_name": "Ada",
            "timezone": "Europe/Berlin",
            "write_enabled": False,
            "auto_approve_writes": False,
            "auto_approve_browser": False,
            "shell_enabled": False,
        },
        headers=CSRF,
    )
    assert updated.status_code == 200
    preferences = updated.json()["preferences"]
    assert preferences["agent_name"] == "Ada"
    assert preferences["write_enabled"] is False
    assert preferences["auto_approve_browser"] is False
    assert preferences["shell_enabled"] is False

    session = auth_client.get("/api/session").json()
    assert session["agent_name"] == "Ada"
    assert session["timezone"] == "Europe/Berlin"


def test_chat_status_tracks_active_run(app, auth_client):
    queue = app.state.services.chat_queue
    queue.try_begin("status-chat")
    active = auth_client.get("/api/chat/status?chat_id=status-chat").json()
    assert active["active"] is True
    queue.finish("status-chat")
    idle = auth_client.get("/api/chat/status?chat_id=status-chat").json()
    assert idle["active"] is False


def test_chat_sends_uploaded_document_to_the_model(app, auth_client):
    fake = install_fake_ai(app, [FakeMessage(content="Read it.")])
    uploaded = auth_client.post(
        "/api/uploads",
        files={"file": ("notes.txt", b"Important upload text.", "text/plain")},
        headers=CSRF,
    ).json()
    response = auth_client.post(
        "/api/chat",
        json={
            "message": "Summarise this.",
            "chat_id": "upload-chat",
            "attachments": [uploaded],
        },
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == "Read it."
    assert fake.chat_calls
    user_messages = [
        item for item in fake.chat_calls[0] if item.get("role") == "user"
    ]
    assert "Important upload text." in user_messages[-1]["content"]


def test_upload_and_fetch_attachment(auth_client):
    response = auth_client.post(
        "/api/uploads",
        files={"file": ("notes.txt", b"hello from an upload", "text/plain")},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    attachment = response.json()
    assert attachment["kind"] == "document"
    assert attachment["url"].startswith("/api/uploads/")
    fetched = auth_client.get(attachment["url"])
    assert fetched.status_code == 200
    assert fetched.content == b"hello from an upload"


def test_upload_rejects_unsupported_file(auth_client):
    response = auth_client.post(
        "/api/uploads",
        files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
        headers=CSRF,
    )
    assert response.status_code == 400


def test_browser_is_disabled_in_tests(auth_client):
    status = auth_client.get("/api/browser/status").json()
    assert status["enabled"] is False
    assert status["cors_mode"] == "off"
    shot = auth_client.get("/api/browser/screenshot")
    assert shot.status_code == 503
    diagnostics = auth_client.get("/api/browser/diagnostics").json()
    assert diagnostics["console"] == []
    assert diagnostics["failed_requests"] == []
    clear = auth_client.post(
        "/api/browser/clear-site",
        json={"domain": "github.com"},
        headers=CSRF,
    )
    assert clear.status_code == 503


def test_csp_allows_configured_frame_sources(settings):
    settings.browser_vnc_url = (
        "http://192.168.1.6:6080/vnc.html?autoconnect=1&resize=scale"
    )
    settings.csp_frame_src = (
        "http://localhost:6080, https://frames.example.test/path"
    )

    csp = build_security_headers(settings)["Content-Security-Policy"]

    assert "frame-src" in csp
    assert "http://127.0.0.1:6080" in csp
    assert "http://localhost:6080" in csp
    assert "http://192.168.1.6:6080" in csp
    assert "https://frames.example.test" in csp
    assert "frames.example.test/path" not in csp
    assert "javascript:" not in csp
    assert "frame-ancestors 'none'" in csp


def test_csp_allows_request_host_for_vnc_view(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings.browser_vnc_url = "http://192.168.1.6:6080/vnc.html?autoconnect=1"
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz", headers={"Host": "muse.example.test"})
    csp = response.headers["Content-Security-Policy"]
    assert "http://192.168.1.6:6080" in csp  # configured origin stays allowed
    assert "http://muse.example.test:6080" in csp  # request host added


def test_browser_status_view_url_follows_request_host(app, auth_client):
    services = app.state.services
    services.settings.browser_vnc_url = (
        "http://192.168.1.6:6080/vnc.html?autoconnect=1"
    )
    status = auth_client.get(
        "/api/browser/status", headers={"Host": "muse.example.test"}
    ).json()
    assert status["view_url"] == (
        "http://muse.example.test:6080/vnc.html?autoconnect=1&view_only=1"
    )

    plain = auth_client.get("/api/browser/status").json()
    # The request's Host header is always present in real traffic, so the
    # rewrite applies there too (TestClient sends "testserver"), and the view
    # stays view-only until the user takes control.
    assert plain["view_url"] == (
        "http://testserver:6080/vnc.html?autoconnect=1&view_only=1"
    )


def test_static_assets_revalidate(auth_client):
    response = auth_client.get("/static/app.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"


def test_index_stamps_the_build_and_session_reports_it(client):
    from app import BUILD

    page = client.get("/")
    assert page.status_code == 200
    assert page.headers.get("cache-control") == "no-store"
    assert "__BUILD__" not in page.text
    assert "/static/app.js?v=%s" % BUILD in page.text
    assert "/static/styles.css?v=%s" % BUILD in page.text
    assert 'name="muse-build" content="%s"' % BUILD in page.text

    session = client.get("/api/session").json()
    assert session["build"] == BUILD


def test_csp_ignores_unsafe_frame_values(settings):
    settings.browser_vnc_url = "javascript:alert(1)"
    settings.csp_frame_src = "*, data:text/html,http://ok.example:6080/path"

    csp = build_security_headers(settings)["Content-Security-Policy"]

    assert "http://ok.example:6080" in csp
    assert "javascript:" not in csp
    assert "data:text/html" not in csp
    assert "frame-src *;" not in csp
