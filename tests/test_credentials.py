from __future__ import annotations

from typing import Any, Dict, List

from conftest import CSRF, install_fake_ai

from app.tools import execute_tool

MASTER = "credential master password"


class FakeBrowser:
    def __init__(self, url: str = "https://example.com/login") -> None:
        self.url = url
        self.filled: List[Dict[str, Any]] = []

    def status(self, session: str = "default") -> Dict[str, Any]:
        return {"enabled": True, "started": True, "url": self.url}

    async def detect_login_fields(
        self, session: str = "default"
    ) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": "Sign in",
            "username": "#username",
            "password": "#password",
            "submit": "#submit",
            "form_action": self.url,
        }

    async def fill_credential(
        self,
        label: str,
        username_selector: str = "",
        password_selector: str = "",
        submit_selector: str = "",
        session: str = "default",
    ) -> Dict[str, Any]:
        self.filled.append(
            {
                "label": label,
                "username_selector": username_selector,
                "password_selector": password_selector,
                "submit_selector": submit_selector,
            }
        )
        return {
            "filled_fields": 2,
            "submitted": True,
            "submit_method": "button",
            "autodetected": not bool(password_selector),
            "url": "https://example.com/home",
            "title": "Home",
            "text": "Welcome back",
        }

    async def close(self) -> None:
        return None


def prepare(app):
    services = app.state.services
    services.vault.initialize(MASTER)
    browser = FakeBrowser()
    services.browser = browser
    return services, browser


def test_fulfill_creates_entry_and_logs_in(app, auth_client):
    services, browser = prepare(app)
    install_fake_ai(app, ["Welcome back. You are signed in."])
    request = services.credentials.create(
        "chat-cred",
        "Example",
        "example.com",
        "https://example.com/login",
        "Muse needs to sign in to continue.",
        username_selector="#username",
        password_selector="#password",
        submit_selector="#submit",
    )

    response = auth_client.post(
        "/api/credential-requests/%d/fulfill" % request["id"],
        json={
            "label": "Example",
            "username": "me@example.com",
            "password": "hunter2-super-secret",
            "notes": "personal",
            "trust_host": True,
        },
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["login_ok"] is True
    assert data["request"]["status"] == "fulfilled"
    assert data["followup"]["reply"] == "Welcome back. You are signed in."
    assert "hunter2-super-secret" not in response.text

    secret = services.vault.get_secret_by_label("Example")
    assert secret["username"] == "me@example.com"
    assert secret["password"] == "hunter2-super-secret"
    assert secret["domains"] == ["example.com"]
    assert browser.filled[0]["label"] == "Example"
    assert browser.filled[0]["password_selector"] == "#password"
    assert [item["host"] for item in data["trusted_hosts"]] == ["example.com"]
    assert services.trust.is_trusted("example.com") is True

    audit = services.audit.recent(50)
    assert "hunter2-super-secret" not in str(audit)
    assert any(row["action"] == "credential_fulfilled" for row in audit)


def test_fulfill_refuses_wrong_domain(app, auth_client):
    services, browser = prepare(app)
    browser.url = "https://evil.example.net/login"
    request = services.credentials.create(
        "chat-cred", "Example", "example.com", "https://example.com/login"
    )
    response = auth_client.post(
        "/api/credential-requests/%d/fulfill" % request["id"],
        json={"label": "Example", "username": "me", "password": "secret"},
        headers=CSRF,
    )
    assert response.status_code == 409
    assert "no longer on" in response.json()["detail"]
    assert services.vault.list_entries() == []


def test_fulfill_requires_unlocked_vault(app, auth_client):
    services, _ = prepare(app)
    services.vault.lock()
    request = services.credentials.create(
        "chat-cred", "Example", "example.com", "https://example.com/login"
    )
    response = auth_client.post(
        "/api/credential-requests/%d/fulfill" % request["id"],
        json={"label": "Example", "username": "me", "password": "secret"},
        headers=CSRF,
    )
    assert response.status_code == 423
    assert services.credentials.get(request["id"])["status"] == "pending"


def test_cancel_credential_request(app, auth_client):
    services, _ = prepare(app)
    request = services.credentials.create(
        "chat-cred", "Example", "example.com", "https://example.com/login"
    )
    response = auth_client.post(
        "/api/credential-requests/%d/cancel" % request["id"],
        json={"reason": "Not now"},
        headers=CSRF,
    )
    assert response.status_code == 200
    assert response.json()["request"]["status"] == "cancelled"
    assert services.credentials.get(request["id"])["result"] == "Not now"


def test_list_credential_requests_hides_secrets(app, auth_client):
    services, _ = prepare(app)
    request = services.credentials.create(
        "chat-cred", "Example", "example.com", "https://example.com/login"
    )
    data = auth_client.get(
        "/api/credential-requests?chat_id=chat-cred&status=pending"
    ).json()
    assert [item["id"] for item in data["requests"]] == [request["id"]]
    assert data["vault"]["unlocked"] is True
    assert "password" not in data["requests"][0]


async def test_request_credential_tool_creates_card(app):
    services, _ = prepare(app)
    result = await execute_tool(
        services,
        "request_credential",
        {"label": "Example", "reason": "Need the account login"},
        chat_id="chat-tool",
    )
    assert result["ok"] is True
    payload = result["result"]
    assert payload["status"] == "credential_required"
    request = services.credentials.get(payload["request_id"])
    assert request["domain"] == "example.com"
    assert request["chat_id"] == "chat-tool"
    assert request["password_selector"] == "#password"
    assert request["reason"] == "Need the account login"
    assert request["status"] == "pending"


async def test_request_credential_returns_existing_entries(app):
    services, _ = prepare(app)
    services.vault.add_entry("Example", ["example.com"], password="secret")
    result = await execute_tool(
        services, "request_credential", {"reason": "Need login"}, chat_id="chat-tool"
    )
    payload = result["result"]
    assert payload["status"] == "existing_credentials"
    assert payload["entries"][0]["label"] == "Example"

