"""OpenAI client wrapper for text, tool calling and image generation."""
from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openai import OpenAI

from .config import Settings
from .providers import ProviderConfig, ProviderStore


class OpenAIConfigError(RuntimeError):
    pass


class OpenAIService:
    def __init__(
        self, settings: Settings, providers: Optional[ProviderStore] = None
    ) -> None:
        self.settings = settings
        self.providers = providers
        self._client: Optional[OpenAI] = None
        self._client_signature: Optional[Tuple[Any, ...]] = None
        self._image_client: Optional[OpenAI] = None
        self._image_signature: Optional[Tuple[Any, ...]] = None

    def _config(self) -> ProviderConfig:
        if self.providers is not None:
            return self.providers.current()
        return ProviderConfig.from_settings(self.settings)

    @property
    def configured(self) -> bool:
        return bool(self._config().api_key)

    def invalidate(self) -> None:
        self._client = None
        self._client_signature = None
        self._image_client = None
        self._image_signature = None

    def info(self) -> Dict[str, Any]:
        config = self._config()
        return {
            "configured": bool(config.api_key),
            "model": config.text_model,
            "base_url": config.base_url or "",
            "source": config.source,
            "api_key_error": config.api_key_error,
        }

    def _make_client(
        self, api_key: str, base_url: Optional[str], user_agent: str
    ) -> OpenAI:
        if not api_key:
            raise OpenAIConfigError(
                "No API key is configured. Add one in Provider settings or .env."
            )
        kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "timeout": self.settings.openai_timeout_seconds,
            "max_retries": 2,
        }
        if base_url:
            kwargs["base_url"] = base_url
        if user_agent:
            kwargs["default_headers"] = {"User-Agent": user_agent}
        return OpenAI(**kwargs)

    def client(self) -> OpenAI:
        config = self._config()
        if not config.api_key:
            raise OpenAIConfigError(
                "No API key is configured. Add one in Provider settings or .env."
            )
        signature = (config.api_key, config.base_url, config.user_agent)
        if self._client is None or self._client_signature != signature:
            self._client = self._make_client(
                config.api_key, config.base_url, config.user_agent
            )
            self._client_signature = signature
        return self._client

    def image_client(self) -> OpenAI:
        """Client for image generation, optionally a different provider."""
        config = self._config()
        image_key = config.image_api_key or config.api_key
        image_base = config.image_base_url or config.base_url
        if not image_key:
            raise OpenAIConfigError(
                "No image API key is configured. Add one in Provider settings."
            )
        if image_key == config.api_key and image_base == config.base_url:
            return self.client()
        signature = (image_key, image_base, config.user_agent)
        if self._image_client is None or self._image_signature != signature:
            self._image_client = self._make_client(
                image_key, image_base, config.user_agent
            )
            self._image_signature = signature
        return self._image_client

    def _extra_headers(
        self, config: ProviderConfig, session_id: Optional[str]
    ) -> Optional[Dict[str, str]]:
        """OpenCode Go requires a stable session header for routing/caching."""
        header = (config.session_header or "").strip()
        if not header or not session_id:
            return None
        return {header: str(session_id)[:200]}

    # -- chat -------------------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
    ) -> Any:
        kwargs: Dict[str, Any] = {
            "model": (model or "").strip() or self._config().text_model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if temperature is not None:
            kwargs["temperature"] = temperature
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        return self.client().chat.completions.create(**kwargs)

    async def achat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Any:
        return await asyncio.to_thread(
            self.chat,
            messages,
            tools,
            temperature,
            self._extra_headers(self._config(), session_id),
            model,
        )

    async def agenerate_text(
        self,
        prompt: str,
        system: Optional[str] = None,
        max_words: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> str:
        instruction = (prompt or "").strip()
        if not instruction:
            raise ValueError("Prompt is required.")
        if max_words:
            instruction += "\n\nKeep the result under %d words." % int(max_words)
        messages = [
            {
                "role": "system",
                "content": system
                or (
                    "You are a precise writing assistant. Produce the requested "
                    "text directly, with no preamble about being an AI."
                ),
            },
            {"role": "user", "content": instruction},
        ]
        response = await asyncio.to_thread(
            self.chat,
            messages,
            None,
            None,
            self._extra_headers(self._config(), session_id),
        )
        content = response.choices[0].message.content
        return content or ""

    # -- model discovery ---------------------------------------------------
    async def list_models(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        user_agent: Optional[str] = None,
        session_header: Optional[str] = None,
    ) -> List[str]:
        """Query the OpenAI-protocol discovery endpoint (GET /models).

        Optional arguments override the saved provider configuration so the
        UI can list models from an endpoint the user is still typing in.
        """
        config = self._config()
        key = (api_key or "").strip() or config.api_key
        if not key:
            raise OpenAIConfigError(
                "No API key is configured. Add one in Provider settings or .env."
            )
        client = self._make_client(
            key,
            (base_url or "").strip() or config.base_url,
            (user_agent or "").strip() or config.user_agent,
        )
        header = (session_header or "").strip() or config.session_header
        kwargs: Dict[str, Any] = {}
        if header:
            kwargs["extra_headers"] = {header: "muse-model-list"}
        response = await asyncio.to_thread(client.models.list, **kwargs)
        ids = {
            str(item.id)
            for item in (getattr(response, "data", None) or [])
            if getattr(item, "id", None)
        }
        return sorted(ids)

    # -- images -----------------------------------------------------------
    def generate_image(self, prompt: str, size: Optional[str] = None) -> Dict[str, str]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Prompt is required.")
        response = self.image_client().images.generate(
            model=self._config().image_model,
            prompt=prompt,
            size=size or self._config().image_size,
            n=1,
        )
        if not response.data:
            raise RuntimeError("The image API returned no data.")
        item = response.data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            raw = base64.b64decode(b64)
        elif getattr(item, "url", None):
            with httpx.Client(timeout=90, follow_redirects=True) as http:
                resp = http.get(item.url)
                resp.raise_for_status()
                raw = resp.content
        else:  # pragma: no cover - defensive
            raise RuntimeError("The image API returned neither b64_json nor url.")
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        filename = "image-%s.png" % uuid.uuid4().hex
        path = self.settings.artifacts_dir / filename
        path.write_bytes(raw)
        return {"filename": filename, "path": str(path)}

    async def agenerate_image(
        self, prompt: str, size: Optional[str] = None
    ) -> Dict[str, str]:
        return await asyncio.to_thread(self.generate_image, prompt, size)
