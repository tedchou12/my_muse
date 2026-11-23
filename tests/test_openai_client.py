from __future__ import annotations

import types
from typing import Any, Dict

from app.config import Settings
from app.openai_client import OpenAIService


class DummyCompletions:
    def __init__(self) -> None:
        self.kwargs: Dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        message = types.SimpleNamespace(content="ok", tool_calls=[])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class DummyClient:
    def __init__(self) -> None:
        self.chat = types.SimpleNamespace(completions=DummyCompletions())


def test_client_uses_base_url_and_custom_user_agent(monkeypatch, settings):
    settings.openai_base_url = "https://opencode.ai/inference/go/openai/v1"
    settings.openai_user_agent = "Muse/0.1.0"
    captured: Dict[str, Any] = {}

    class Capture:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("app.openai_client.OpenAI", Capture)
    service = OpenAIService(settings)
    service.client()

    assert captured["base_url"] == "https://opencode.ai/inference/go/openai/v1"
    assert captured["default_headers"]["User-Agent"] == "Muse/0.1.0"
    assert captured["api_key"] == settings.openai_api_key


async def test_session_header_is_sent(settings):
    settings.openai_session_header = "x-opencode-session"
    service = OpenAIService(settings)
    dummy = DummyClient()
    service.client = lambda: dummy

    await service.achat(
        [{"role": "user", "content": "hi"}], session_id="chat-42"
    )
    assert dummy.chat.completions.kwargs["extra_headers"] == {
        "x-opencode-session": "chat-42"
    }


async def test_no_session_header_when_not_configured(settings):
    settings.openai_session_header = ""
    service = OpenAIService(settings)
    dummy = DummyClient()
    service.client = lambda: dummy

    await service.achat([{"role": "user", "content": "hi"}], session_id="chat-42")
    assert "extra_headers" not in dummy.chat.completions.kwargs


def test_image_client_can_use_separate_provider(monkeypatch, settings):
    settings.openai_image_api_key = "sk-image-provider"
    settings.openai_image_base_url = "https://opencode.ai/inference/openai/v1"
    captured = []

    class Capture:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

    monkeypatch.setattr("app.openai_client.OpenAI", Capture)
    service = OpenAIService(settings)
    service.image_client()

    assert captured[0]["api_key"] == "sk-image-provider"
    assert captured[0]["base_url"] == "https://opencode.ai/inference/openai/v1"


def test_image_client_falls_back_to_main_client(settings):
    service = OpenAIService(settings)
    assert service.image_client() is service.client()
