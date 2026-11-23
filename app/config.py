"""Application configuration (environment variables / .env file)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Muse"

    # --- Single-user authentication -------------------------------------
    app_password: str = ""
    session_secret: str = ""
    cookie_secure: bool = False
    session_ttl_seconds: int = 43_200
    login_max_attempts: int = 5
    login_window_seconds: int = 60

    # --- Storage / agent behaviour --------------------------------------
    data_dir: Path = Path("data")
    scheduler_enabled: bool = True
    scheduler_timezone: str = "UTC"
    scheduler_tick_seconds: float = 2.0
    scheduler_max_concurrent: int = 2
    max_agent_steps: int = 0  # 0 = no fixed round limit
    agent_run_timeout_seconds: float = 900.0  # wall-clock safety, 0 = disabled
    team_enabled: bool = True
    team_auto_run: bool = True
    team_tick_seconds: float = 2.0
    team_max_concurrent: int = 2
    team_max_agents: int = 16
    team_max_depth: int = 3
    vault_idle_lock_seconds: int = 900

    # --- OpenAI ----------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: Optional[str] = None
    openai_text_model: str = "gpt-4o-mini"
    openai_image_model: str = "gpt-image-1"
    image_size: str = "1024x1024"
    openai_timeout_seconds: float = 120.0
    openai_user_agent: str = "Muse/0.1.0"
    openai_session_header: str = ""
    openai_image_base_url: Optional[str] = None
    openai_image_api_key: str = ""

    # --- Browser sandbox -------------------------------------------------
    browser_enabled: bool = True
    browser_headless: bool = True
    browser_allowed_domains: str = ""
    browser_allow_private_networks: bool = False
    browser_nav_timeout_ms: int = 30_000
    browser_cors_mode: str = "off"  # off | inject
    browser_cors_domains: str = ""
    browser_block_service_workers: bool = False
    browser_cdp_url: str = ""
    browser_vnc_url: str = ""
    csp_frame_src: str = ""
    browser_connect_timeout_ms: int = 15_000

    # --- Sentinel defaults (editable in the UI) --------------------------
    write_enabled: bool = True
    auto_approve_writes: bool = False
    shell_enabled: bool = False
    shell_workspace_dir: Optional[Path] = None
    shell_timeout_seconds: int = 60

    log_level: str = "INFO"

    # ---------------------------------------------------------------------
    @property
    def allowed_domains(self) -> List[str]:
        return [
            item.strip().lower()
            for item in self.browser_allowed_domains.split(",")
            if item.strip()
        ]

    @property
    def cors_domains(self) -> List[str]:
        return [
            item.strip().lower()
            for item in self.browser_cors_domains.split(",")
            if item.strip()
        ]

    @property
    def database_path(self) -> Path:
        return self.data_dir / "muse.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def browser_dir(self) -> Path:
        return self.data_dir / "browser"

    @property
    def shell_workspace(self) -> Path:
        return self.shell_workspace_dir or (self.data_dir / "workspace")

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.artifacts_dir,
            self.uploads_dir,
            self.browser_dir,
            self.browser_dir / "screenshots",
            self.browser_dir / "profile",
            self.shell_workspace,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def validate_runtime(self) -> None:
        """Fail fast instead of booting an unauthenticated agent."""
        if self.browser_cors_mode not in ("off", "inject"):
            raise RuntimeError(
                "BROWSER_CORS_MODE must be 'off' or 'inject'."
            )
        if self.browser_cdp_url and not self.browser_cdp_url.startswith(
            ("http://", "https://", "ws://", "wss://")
        ):
            raise RuntimeError(
                "BROWSER_CDP_URL must be an http(s), ws(s) or CDP URL."
            )
        if not self.app_password:
            raise RuntimeError(
                "APP_PASSWORD is not set. Muse would be an open proxy to your "
                "OpenAI account. Copy .env.example to .env and set a password."
            )
        if len(self.app_password) < 8:
            raise RuntimeError("APP_PASSWORD must be at least 8 characters.")
        if not self.session_secret:
            # Deriving the signing key keeps single-container deployments simple.
            # Changing APP_PASSWORD invalidates existing sessions.
            self.session_secret = hashlib.sha256(
                ("muse-session-v1:" + self.app_password).encode("utf-8")
            ).hexdigest()
