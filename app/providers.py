"""Runtime provider configuration.

The UI can switch Muse between OpenAI, OpenCode Go, OpenCode Console and any
custom OpenAI-compatible gateway without editing .env or recreating the
container. Provider API keys are encrypted at rest with AES-256-GCM using a key
derived from SESSION_SECRET (or APP_PASSWORD when no explicit secret is set).

If SESSION_SECRET/APP_PASSWORD changes, stored provider keys become unreadable;
Muse reports that in the UI and falls back to environment values.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .audit import AuditLog
from .config import Settings
from .db import Database, now_ts

STORE_KEY = "provider_config"
ALLOWED_PRESETS = (
    "openai",
    "openrouter",
    "claude",
    "deepseek",
    "opencode_go",
    "opencode_console",
    "custom",
)


@dataclass(frozen=True)
class ProviderConfig:
    preset: str
    base_url: Optional[str]
    api_key: str
    text_model: str
    user_agent: str
    session_header: str
    image_base_url: Optional[str]
    image_api_key: str
    image_model: str
    image_size: str
    source: str = "environment"
    api_key_error: bool = False
    image_api_key_error: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProviderConfig":
        base = settings.openai_base_url or ""
        if "openrouter.ai" in base:
            preset = "openrouter"
        elif "api.anthropic.com" in base:
            preset = "claude"
        elif "api.deepseek.com" in base:
            preset = "deepseek"
        elif "opencode.ai/inference/go" in base:
            preset = "opencode_go"
        elif "opencode.ai/inference" in base:
            preset = "opencode_console"
        elif not base:
            preset = "openai"
        else:
            preset = "custom"
        return cls(
            preset=preset,
            base_url=settings.openai_base_url or None,
            api_key=settings.openai_api_key,
            text_model=settings.openai_text_model,
            user_agent=settings.openai_user_agent,
            session_header=settings.openai_session_header,
            image_base_url=settings.openai_image_base_url or None,
            image_api_key=settings.openai_image_api_key,
            image_model=settings.openai_image_model,
            image_size=settings.image_size,
            source="environment",
        )


class ProviderStore:
    def __init__(self, db: Database, settings: Settings, audit: AuditLog) -> None:
        self.db = db
        self.settings = settings
        self.audit = audit
        secret = settings.session_secret or settings.app_password or "muse"
        self._aead = AESGCM(
            hashlib.sha256(("muse-provider-v1:" + secret).encode("utf-8")).digest()
        )

    # -- storage ----------------------------------------------------------
    def _raw(self) -> Optional[Dict[str, Any]]:
        row = self.db.one(
            "SELECT value FROM settings WHERE key = ?", (STORE_KEY,)
        )
        if not row:
            return None
        try:
            data = json.loads(row["value"])
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def has_saved(self) -> bool:
        return self._raw() is not None

    def _encrypt(self, field: str, value: str) -> str:
        if not value:
            return ""
        nonce = secrets.token_bytes(12)
        payload = self._aead.encrypt(nonce, value.encode("utf-8"), field.encode())
        return base64.b64encode(nonce + payload).decode("ascii")

    def _decrypt(self, field: str, blob: Optional[str]) -> Optional[str]:
        if not blob:
            return ""
        try:
            raw = base64.b64decode(blob.encode("ascii"))
            return self._aead.decrypt(
                raw[:12], raw[12:], field.encode()
            ).decode("utf-8")
        except (InvalidTag, ValueError, UnicodeDecodeError, TypeError):
            return None

    # -- runtime ----------------------------------------------------------
    def current(self) -> ProviderConfig:
        data = self._raw()
        if data is None:
            return ProviderConfig.from_settings(self.settings)

        api_key = self._decrypt("api_key", data.get("api_key_enc", ""))
        image_key = self._decrypt(
            "image_api_key", data.get("image_api_key_enc", "")
        )
        return ProviderConfig(
            preset=data.get("preset") or "custom",
            base_url=data.get("base_url") or None,
            api_key=api_key or "",
            text_model=data.get("text_model") or self.settings.openai_text_model,
            user_agent=data.get("user_agent") or self.settings.openai_user_agent,
            session_header=data.get("session_header") or "",
            image_base_url=data.get("image_base_url") or None,
            image_api_key=image_key or "",
            image_model=data.get("image_model") or self.settings.openai_image_model,
            image_size=data.get("image_size") or self.settings.image_size,
            source="database",
            api_key_error=api_key is None,
            image_api_key_error=image_key is None,
        )

    def public(self) -> Dict[str, Any]:
        config = self.current()
        return {
            "preset": config.preset,
            "base_url": config.base_url or "",
            "text_model": config.text_model,
            "user_agent": config.user_agent,
            "session_header": config.session_header,
            "image_base_url": config.image_base_url or "",
            "image_model": config.image_model,
            "image_size": config.image_size,
            "api_key_set": bool(config.api_key),
            "image_api_key_set": bool(config.image_api_key),
            "source": config.source,
            "configured": bool(config.api_key),
            "api_key_error": config.api_key_error,
            "image_api_key_error": config.image_api_key_error,
            "has_saved_config": self.has_saved(),
        }

    # -- updates ----------------------------------------------------------
    def update(self, values: Dict[str, Any]) -> Dict[str, Any]:
        existing = self._raw() or {}

        api_key_enc = existing.get("api_key_enc", "")
        if values.get("clear_api_key"):
            api_key_enc = ""
        elif values.get("api_key"):
            api_key_enc = self._encrypt("api_key", str(values["api_key"]).strip())
        elif not api_key_enc and self.settings.openai_api_key:
            # First save from the UI adopts the environment key into the
            # encrypted store, so the database becomes authoritative.
            api_key_enc = self._encrypt(
                "api_key", self.settings.openai_api_key
            )

        image_key_enc = existing.get("image_api_key_enc", "")
        if values.get("clear_image_api_key"):
            image_key_enc = ""
        elif values.get("image_api_key"):
            image_key_enc = self._encrypt(
                "image_api_key", str(values["image_api_key"]).strip()
            )
        elif not image_key_enc and self.settings.openai_image_api_key:
            image_key_enc = self._encrypt(
                "image_api_key", self.settings.openai_image_api_key
            )

        data = {
            "preset": values.get("preset") or "custom",
            "base_url": (values.get("base_url") or "").strip(),
            "api_key_enc": api_key_enc,
            "text_model": (values.get("text_model") or "").strip()
            or self.settings.openai_text_model,
            "user_agent": (values.get("user_agent") or "").strip()
            or self.settings.openai_user_agent,
            "session_header": (values.get("session_header") or "").strip(),
            "image_base_url": (values.get("image_base_url") or "").strip(),
            "image_api_key_enc": image_key_enc,
            "image_model": (values.get("image_model") or "").strip()
            or self.settings.openai_image_model,
            "image_size": (values.get("image_size") or "").strip()
            or self.settings.image_size,
            "updated_at": now_ts(),
        }
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (STORE_KEY, json.dumps(data)),
        )
        self.audit.log(
            "user",
            "provider_update",
            {
                "preset": data["preset"],
                "base_url": data["base_url"],
                "text_model": data["text_model"],
                "api_key_set": bool(api_key_enc),
                "image_api_key_set": bool(image_key_enc),
            },
        )
        return self.public()

