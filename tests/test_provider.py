from __future__ import annotations

import pytest

from conftest import CSRF, FakeOpenAI, install_fake_ai
from app.providers import ALLOWED_PRESETS, ProviderConfig

GO_PAYLOAD = {
    "preset": "opencode_go",
    "base_url": "https://opencode.ai/inference/go/openai/v1",
    "text_model": "kimi-k3",
    "user_agent": "Muse/0.1.0",
    "session_header": "x-opencode-session",
    "image_base_url": "",
    "image_model": "gpt-image-1",
    "image_size": "1024x1024",
}
SECRET = "oc_sk_test_secret_123456"


def test_provider_supports_openrouter_claude_and_deepseek(settings):
    assert {"openrouter", "claude", "deepseek"} <= set(ALLOWED_PRESETS)

    settings.openai_base_url = "https://openrouter.ai/api/v1"
    assert ProviderConfig.from_settings(settings).preset == "openrouter"

    settings.openai_base_url = "https://api.anthropic.com/v1"
    assert ProviderConfig.from_settings(settings).preset == "claude"

    settings.openai_base_url = "https://api.deepseek.com"
    assert ProviderConfig.from_settings(settings).preset == "deepseek"


def test_provider_starts_from_environment(app, auth_client):
    data = auth_client.get("/api/provider").json()
    provider = data["provider"]
    assert provider["source"] == "environment"
    assert provider["configured"] is True
    assert provider["api_key_set"] is True
    assert "sk-test-key" not in str(data)


def test_provider_update_encrypts_key(app, auth_client):
    response = auth_client.put(
        "/api/provider",
        json=dict(GO_PAYLOAD, api_key=SECRET),
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    provider = response.json()["provider"]
    assert provider["source"] == "database"
    assert provider["configured"] is True
    assert provider["api_key_set"] is True
    assert SECRET not in response.text

    fetched = auth_client.get("/api/provider")
    assert SECRET not in fetched.text
    assert fetched.json()["provider"]["text_model"] == "kimi-k3"

    row = app.state.services.db.one(
        "SELECT value FROM settings WHERE key = 'provider_config'"
    )
    assert SECRET not in row["value"]
    assert app.state.services.providers.current().api_key == SECRET
    assert (
        app.state.services.providers.current().base_url
        == "https://opencode.ai/inference/go/openai/v1"
    )

    audit = auth_client.get("/api/activity").json()
    updates = [item for item in audit["audit"] if item["action"] == "provider_update"]
    assert updates
    assert SECRET not in updates[0]["detail"]


def test_provider_clear_key(app, auth_client):
    auth_client.put(
        "/api/provider", json=dict(GO_PAYLOAD, api_key=SECRET), headers=CSRF
    )
    cleared = auth_client.put(
        "/api/provider",
        json=dict(GO_PAYLOAD, clear_api_key=True),
        headers=CSRF,
    )
    assert cleared.status_code == 200
    assert cleared.json()["provider"]["api_key_set"] is False
    assert cleared.json()["provider"]["configured"] is False
    assert app.state.services.providers.current().api_key == ""


def test_provider_validation(auth_client):
    bad_preset = auth_client.put(
        "/api/provider", json=dict(GO_PAYLOAD, preset="nope"), headers=CSRF
    )
    assert bad_preset.status_code == 400

    bad_url = auth_client.put(
        "/api/provider",
        json=dict(GO_PAYLOAD, base_url="ftp://example.com"),
        headers=CSRF,
    )
    assert bad_url.status_code == 400

    bad_header = auth_client.put(
        "/api/provider",
        json=dict(GO_PAYLOAD, session_header="bad header"),
        headers=CSRF,
    )
    assert bad_header.status_code == 400

    ambiguous = auth_client.put(
        "/api/provider",
        json=dict(GO_PAYLOAD, api_key=SECRET, clear_api_key=True),
        headers=CSRF,
    )
    assert ambiguous.status_code == 400


def test_provider_test_endpoint(app, auth_client):
    install_fake_ai(app, ["MUSE_OK"])
    auth_client.put(
        "/api/provider", json=dict(GO_PAYLOAD, api_key=SECRET), headers=CSRF
    )
    result = auth_client.post("/api/provider/test", headers=CSRF)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["ok"] is True
    assert data["model"] == "kimi-k3"
    assert data["reply"] == "MUSE_OK"


def test_provider_key_unreadable_after_secret_change(app, auth_client):
    auth_client.put(
        "/api/provider", json=dict(GO_PAYLOAD, api_key=SECRET), headers=CSRF
    )
    services = app.state.services
    changed = services.settings.model_copy(
        update={"session_secret": "a-different-session-secret"}
    )
    from app.providers import ProviderStore

    other = ProviderStore(services.db, changed, services.audit)
    config = other.current()
    assert config.api_key == ""
    assert config.api_key_error is True
    assert other.public()["configured"] is False


def test_openai_service_uses_saved_provider(monkeypatch, app, auth_client):
    from app.openai_client import OpenAIService

    auth_client.put(
        "/api/provider", json=dict(GO_PAYLOAD, api_key=SECRET), headers=CSRF
    )
    services = app.state.services
    captured = {}

    class Capture:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("app.openai_client.OpenAI", Capture)
    service = OpenAIService(services.settings, services.providers)
    service.client()
    assert captured["api_key"] == SECRET
    assert captured["base_url"] == GO_PAYLOAD["base_url"]
    assert captured["default_headers"]["User-Agent"] == "Muse/0.1.0"
    assert service.configured is True
