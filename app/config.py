"""Typed application settings. Every tunable lives here or in versioned YAML."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-change-me-dev-only-change-me"


class ConfigurationError(RuntimeError):
    """Raised at startup when the configuration is unsafe for the selected environment."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "prod", "test"] = "dev"
    # Demo mode seeds the four demo accounts and allows password-less sign-in to them. It is refused
    # in production: a role picker is not authentication.
    demo_mode: bool = True
    secret_key: SecretStr = Field(default=SecretStr(_DEV_SECRET))
    session_ttl_seconds: int = 8 * 3600

    # Persistence. Empty means the in-memory store (development and unit tests only).
    # postgresql+psycopg://user:pass@host:5432/artha  - or sqlite:///path for local single-process use.
    database_url: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    # Pipeline execution. "inline" runs cases on a thread inside the API process (development);
    # "queue" writes a durable job that a separate `python -m app.worker` process claims.
    pipeline_mode: Literal["inline", "queue"] = "inline"
    job_lease_seconds: int = 300
    job_max_attempts: int = 3
    worker_poll_seconds: float = 1.0
    retention_sweep_interval_seconds: int = 3600

    # AI mode: "vertex" = Gemini on Gemini Enterprise Agent Platform (ADC on Cloud Run),
    # "api_key" = Gemini API for local development, "offline" = deterministic simulation.
    llm_mode: Literal["vertex", "api_key", "ollama", "offline"] = "offline"
    # Local models via Ollama. The vision model is optional: without one, scanned or photographed
    # statements are refused with a clear message instead of being guessed at.
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    ollama_vision_model: str | None = None
    ollama_num_ctx: int = 8192
    google_cloud_project: str | None = None
    google_cloud_location: str = "global"
    gemini_api_key: SecretStr | None = None
    model_flash: str = "gemini-3.8-flash"
    model_reasoning: str = "gemini-3.8-flash"
    model_lite: str = "gemini-3.5-flash-lite"
    llm_timeout_seconds: float = 90.0
    token_budget_per_case: int = 400_000

    # Model Armor (optional; enabled when a template name is configured)
    model_armor_template: str | None = None  # projects/<p>/locations/<l>/templates/<id>
    model_armor_location: str = "asia-south1"

    # Limits
    max_upload_mb: int = 10
    max_pages_per_document: int = 30
    max_documents_per_case: int = 3
    max_cases_in_memory: int = 300
    pipeline_concurrency: int = 2
    queue_page_size: int = 200

    # Accounts
    allow_self_registration: bool = True
    password_min_length: int = 12
    login_max_failures: int = 5
    login_lockout_seconds: int = 900

    # Ed25519 seed (32 bytes, base64) used to sign portable decision receipts. Without it the
    # service generates an ephemeral key and says so: receipts will not verify after a restart.
    receipt_signing_seed: SecretStr | None = None
    receipt_verify_url: str = "/.well-known/artha-receipt-key.json"
    case_retention_days: int = 30

    # Origins allowed to make state-changing requests, comma separated. The service's own origin is
    # always allowed.
    web_origin: str | None = None
    trust_proxy_headers: bool = False
    supported_languages: list[str] = ["en", "hi", "id"]

    @property
    def ai_enabled(self) -> bool:
        return self.llm_mode != "offline"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def allowed_origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in (self.web_origin or "").split(",") if o.strip()}

    @model_validator(mode="after")
    def _production_guard(self) -> Settings:
        """Refuse to start in production with settings that are only acceptable on a laptop."""
        if self.app_env != "prod":
            return self
        problems: list[str] = []
        secret = self.secret_key.get_secret_value()
        if secret == _DEV_SECRET or len(secret) < 32:
            problems.append("SECRET_KEY must be a random value of at least 32 characters")
        if self.demo_mode:
            problems.append("DEMO_MODE must be false: demo sign-in is not authentication")
        if not self.database_url or not self.database_url.startswith("postgresql"):
            problems.append("DATABASE_URL must point at PostgreSQL; the in-memory store loses cases on restart")
        if not self.receipt_signing_seed:
            problems.append("RECEIPT_SIGNING_SEED is required; receipts signed by an ephemeral key cannot be verified")
        if self.pipeline_mode != "queue":
            problems.append("PIPELINE_MODE must be 'queue' so a restart cannot lose a case mid-analysis")
        if self.llm_mode == "api_key":
            problems.append("LLM_MODE=api_key is for development; use vertex (service account) or offline")
        if problems:
            raise ConfigurationError("Unsafe production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
