"""Shared fixtures: tmp settings, app, authenticated client, fake OpenAI."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

PASSWORD = "test-password-123"
CSRF = {"X-Muse-Request": "1"}


class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, suffix: str, name: str, args: Dict[str, Any]) -> None:
        self.id = "call_%s" % suffix
        self.type = "function"
        self.function = FakeFunction(name, json.dumps(args))


class FakeMessage:
    def __init__(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[FakeToolCall]] = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


class FakeResponse:
    def __init__(self, message: FakeMessage) -> None:
        self.choices = [FakeChoice(message)]


class FakeOpenAI:
    """Scripted stand-in for OpenAIService."""

    configured = True

    def __init__(self, settings: Settings, script: Optional[List[Any]] = None) -> None:
        self.settings = settings
        self.script: List[Any] = list(script or [])
        self.chat_calls: List[List[Dict[str, Any]]] = []
        self.models_used: List[Optional[str]] = []

    async def achat(
        self, messages, tools=None, temperature=None, session_id=None, model=None
    ):
        self.chat_calls.append(messages)
        self.models_used.append(model)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, FakeMessage):
                return FakeResponse(step)
            return FakeResponse(FakeMessage(content=str(step)))
        return FakeResponse(FakeMessage(content="Done."))

    async def agenerate_text(self, prompt, system=None, max_words=None, session_id=None):
        return "Generated text for: " + prompt

    async def agenerate_image(self, prompt, size=None):
        filename = "test-image.png"
        path = self.settings.artifacts_dir / filename
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
        return {"filename": filename, "path": str(path)}


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        app_password=PASSWORD,
        session_secret="test-session-secret",
        data_dir=tmp_path / "data",
        scheduler_enabled=False,
        browser_enabled=False,
        openai_api_key="sk-test-key",
        openai_base_url=None,
        openai_text_model="gpt-4o-mini",
        openai_user_agent="Muse/0.1.0",
        openai_session_header="",
        openai_image_api_key="",
        openai_image_base_url=None,
        openai_image_model="gpt-image-1",
        image_size="1024x1024",
        log_level="WARNING",
        vault_idle_lock_seconds=3600,
        max_agent_steps=4,
        team_enabled=False,
        team_auto_run=False,
    )


@pytest.fixture()
def app(settings: Settings):
    application = create_app(settings)
    application.state.services.openai = FakeOpenAI(settings)
    return application


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_client(client):
    response = client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture(autouse=True)
def reset_limiters():
    from app import api

    api.login_limiter._events.clear()
    api.vault_limiter._events.clear()
    yield


def install_fake_ai(app, script: List[Any]) -> FakeOpenAI:
    fake = FakeOpenAI(app.state.services.settings, script)
    app.state.services.openai = fake
    return fake
