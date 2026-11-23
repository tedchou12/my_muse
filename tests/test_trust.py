from __future__ import annotations

from typing import Any, Dict

from conftest import CSRF

from app.tools import execute_tool


class FakeBrowser:
    def __init__(self, url: str = "https://example.com/checkout") -> None:
        self.url = url
        self.clicks = 0
        self.fills = 0
        self.last_label = None

    def status(self, session: str = "default") -> Dict[str, Any]:
        return {"enabled": True, "started": True, "url": self.url}

    async def click(self, target: str, session: str = "default") -> Dict[str, Any]:
        self.clicks += 1
        return {"url": self.url, "title": "Page", "text": "clicked"}

    async def type_text(
        self,
        selector: str,
        text: str,
        submit: bool = False,
        session: str = "default",
    ):
        return {"url": self.url, "title": "Page", "text": "typed"}

    async def press(self, key: str, session: str = "default"):
        return {"url": self.url, "title": "Page", "text": "pressed"}

    async def fill_credential(
        self,
        label: str,
        username_selector="",
        password_selector="",
        submit_selector="",
        session: str = "default",
    ):
        self.fills += 1
        self.last_label = label
        return {"filled_fields": 2, "submitted": True, "url": self.url, "title": "Page", "text": "ok"}

    async def close(self) -> None:
        return None


def with_browser(app) -> FakeBrowser:
    browser = FakeBrowser()
    app.state.services.browser = browser
    return browser


async def create_click_approval(app, chat_id: str = "trust-chat"):
    services = app.state.services
    services.prefs.update({"auto_approve_browser": False})
    result = await execute_tool(
        services, "browser_click", {"target": "#buy"}, chat_id=chat_id
    )
    assert result["status"] == "pending_approval"
    return services, result


async def test_routine_browser_actions_auto_approved_by_default(app):
    browser = with_browser(app)
    services = app.state.services
    services.prefs.update({"auto_approve_browser": True})

    result = await execute_tool(
        services, "browser_click", {"target": "#buy"}, chat_id="auto-browser"
    )
    assert result["ok"] is True
    assert result.get("status") is None
    assert browser.clicks == 1
    assert services.approvals.pending() == []


async def test_approve_once_does_not_trust_host(app, auth_client):
    browser = with_browser(app)
    services, result = await create_click_approval(app)
    approval = services.approvals.get(result["approval_id"])
    assert approval["host"] == "example.com"

    response = auth_client.post(
        "/api/approvals/%d/approve" % approval["id"],
        json={"scope": "once"},
        headers=CSRF,
    )
    assert response.status_code == 200
    assert response.json()["trusted_hosts"] == []
    assert browser.clicks == 1

    again = await execute_tool(
        services, "browser_click", {"target": "#buy"}, chat_id="trust-chat"
    )
    assert again["status"] == "pending_approval"
    assert browser.clicks == 1


async def test_approve_all_on_host_then_auto_approve(app, auth_client):
    browser = with_browser(app)
    services, result = await create_click_approval(app)
    approval_id = result["approval_id"]

    response = auth_client.post(
        "/api/approvals/%d/approve" % approval_id,
        json={"scope": "host"},
        headers=CSRF,
    )
    assert response.status_code == 200
    hosts = [item["host"] for item in response.json()["trusted_hosts"]]
    assert hosts == ["example.com"]
    assert browser.clicks == 1

    second = await execute_tool(
        services, "browser_click", {"target": "#buy-again"}, chat_id="trust-chat"
    )
    assert second["ok"] is True
    assert second.get("status") is None
    assert browser.clicks == 2
    assert services.trust.is_trusted("example.com") is True
    assert services.trust.is_trusted("evil.example.com") is False


async def test_host_scope_requires_a_host(app, auth_client):
    browser = with_browser(app)
    services = app.state.services
    result = await execute_tool(
        services, "generate_image", {"prompt": "x"}, chat_id="trust-chat"
    )
    assert result["status"] == "pending_approval"
    approval = services.approvals.get(result["approval_id"])
    assert approval["host"] == ""

    response = auth_client.post(
        "/api/approvals/%d/approve" % approval["id"],
        json={"scope": "host"},
        headers=CSRF,
    )
    assert response.status_code == 400
    assert services.approvals.get(approval["id"])["status"] == "pending"
    assert browser.clicks == 0


async def test_trusted_host_does_not_bypass_vault_fill_approval(app):
    browser = with_browser(app)
    services = app.state.services
    services.trust.trust("example.com")

    result = await execute_tool(
        services, "vault_fill", {}, chat_id="trust-chat"
    )
    assert result["status"] == "pending_approval"
    assert browser.fills == 0
    assert len(services.approvals.pending()) == 1


async def test_revoke_trusted_host(app, auth_client):
    with_browser(app)
    services, result = await create_click_approval(app)
    auth_client.post(
        "/api/approvals/%d/approve" % result["approval_id"],
        json={"scope": "host"},
        headers=CSRF,
    )
    assert services.trust.list()

    response = auth_client.delete(
        "/api/trusted-hosts/example.com", headers=CSRF
    )
    assert response.status_code == 200
    assert response.json()["trusted_hosts"] == []
    assert services.trust.is_trusted("example.com") is False

    again = await execute_tool(
        services, "browser_click", {"target": "#buy"}, chat_id="trust-chat"
    )
    assert again["status"] == "pending_approval"
