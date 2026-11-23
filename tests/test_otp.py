from __future__ import annotations

from typing import Any, Dict

from conftest import CSRF, install_fake_ai

from app.tools import execute_approved, execute_tool


class FakeOtpBrowser:
    def __init__(self) -> None:
        self.url = "https://example.com/challenge"
        self.otp = ""
        self.selectors: Dict[str, str] = {}

    def status(self, session: str = "default") -> Dict[str, Any]:
        return {"enabled": True, "started": True, "url": self.url}

    async def detect_otp_fields(self, session: str = "default") -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": "Verify",
            "otp": "#otp",
            "submit": "#verify",
            "text": "Enter the verification code",
        }

    async def detect_login_fields(self, session: str = "default") -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": "Verify",
            "username": "",
            "password": "#password",
            "submit": "#verify",
        }

    async def fill_credential(self, *args, **kwargs) -> Dict[str, Any]:
        return {
            "filled_fields": 2,
            "submitted": True,
            "otp_submitted": False,
            "otp_error": "",
            "otp_required": True,
            "otp_selector": "#otp",
            "otp_submit_selector": "#verify",
            "entry_label": "Example",
            "url": self.url,
            "title": "Verify",
            "text": "Enter the verification code",
        }

    async def submit_otp(
        self,
        otp: str,
        session: str = "default",
        otp_selector: str = "",
        submit_selector: str = "",
    ) -> Dict[str, Any]:
        self.otp = otp
        self.selectors = {
            "otp_selector": otp_selector,
            "submit_selector": submit_selector,
        }
        return {
            "submitted": True,
            "url": "https://example.com/home",
            "title": "Home",
            "text": "Signed in",
        }

    async def submit_chat_otp(
        self, text: str, session: str = "default"
    ) -> Dict[str, Any]:
        self.otp = text.strip()
        return {
            "submitted": True,
            "url": "https://example.com/home",
            "title": "Home",
            "text": "Signed in",
        }

    async def close(self) -> None:
        return None


def prepare(app) -> FakeOtpBrowser:
    browser = FakeOtpBrowser()
    app.state.services.browser = browser
    return browser


async def test_otp_request_tool_creates_card(app):
    browser = prepare(app)
    services = app.state.services
    result = await execute_tool(
        services,
        "request_otp",
        {"reason": "Google asked for a verification code"},
        chat_id="otp-tool",
    )
    payload = result["result"]
    assert payload["status"] == "otp_required"
    request = services.credentials.get(payload["request_id"])
    assert request["kind"] == "otp"
    assert request["domain"] == "example.com"
    assert request["otp_selector"] == "#otp"
    assert request["submit_selector"] == "#verify"
    assert request["reason"] == "Google asked for a verification code"
    assert browser.otp == ""


def test_fulfill_otp_submits_code_without_storing_it(app, auth_client):
    browser = prepare(app)
    services = app.state.services
    request = services.credentials.create(
        "chat-otp",
        "Example",
        "example.com",
        "https://example.com/challenge",
        "Muse needs the one-time code.",
        kind="otp",
        otp_selector="#otp",
        submit_selector="#verify",
    )
    install_fake_ai(app, ["Verified. Continuing."])
    response = auth_client.post(
        "/api/credential-requests/%d/fulfill" % request["id"],
        json={"otp_code": "654321"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["kind"] == "otp"
    assert data["login_ok"] is True
    assert data["entry"] is None
    assert browser.otp == "654321"
    assert browser.selectors == {
        "otp_selector": "#otp",
        "submit_selector": "#verify",
    }
    assert services.credentials.get(request["id"])["status"] == "fulfilled"
    assert "654321" not in response.text


def test_vault_login_returns_otp_card(app, auth_client):
    prepare(app)
    services = app.state.services
    services.vault.initialize("credential master password")
    request = services.credentials.create(
        "chat-otp",
        "Example",
        "example.com",
        "https://example.com/challenge",
        "Muse needs to sign in.",
    )
    response = auth_client.post(
        "/api/credential-requests/%d/fulfill" % request["id"],
        json={
            "label": "Example",
            "username": "me@example.com",
            "password": "not-stored-in-the-otp-request",
        },
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["login_ok"] is False
    assert data["otp_request"]["kind"] == "otp"
    assert data["otp_request"]["otp_selector"] == "#otp"
    assert services.credentials.get(request["id"])["status"] == "fulfilled"
    assert services.credentials.get(data["otp_request"]["id"])["status"] == "pending"


def test_chat_otp_is_submitted_and_redacted(app, auth_client):
    browser = prepare(app)
    install_fake_ai(app, ["The code was accepted. Continuing."])
    response = auth_client.post(
        "/api/chat",
        json={"message": "654321", "chat_id": "otp-chat"},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["otp_handled"] is True
    assert data["otp_submitted"] is True
    assert browser.otp == "654321"
    history = auth_client.get(
        "/api/chat/history?chat_id=otp-chat"
    ).json()["messages"]
    assert "654321" not in str(history)
    assert any("one-time code" in str(item.get("content", "")).lower() for item in history)


async def test_vault_fill_creates_otp_card(app):
    prepare(app)
    services = app.state.services
    services.prefs.update({"write_enabled": True, "auto_approve_writes": True})
    result = await execute_tool(
        services, "vault_fill", {}, chat_id="otp-vault"
    )
    assert result["status"] == "pending_approval"
    approval = services.approvals.get(result["approval_id"])
    executed = await execute_approved(services, approval)
    payload = executed["result"]
    assert payload["status"] == "otp_required"
    assert payload["otp_required"] is True
    request = services.credentials.get(payload["request_id"])
    assert request["kind"] == "otp"
    assert request["otp_selector"] == "#otp"
    assert request["domain"] == "example.com"
