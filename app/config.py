"""Typed application settings. Every tunable lives here or in versioned YAML."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "prod", "test"] = "dev"
    demo_mode: bool = True
    secret_key: SecretStr = Field(default=SecretStr("dev-only-change-me-dev-only-change-me"))
    session_ttl_seconds: int = 8 * 3600

    # AI mode: "vertex" = Gemini on Gemini Enterprise Agent Platform (ADC on Cloud Run),
    # "api_key" = Gemini API for local development, "offline" = deterministic simulation.
    llm_mode: Literal["vertex", "api_key", "offline"] = "offline"
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

    # Ed25519 seed (32 bytes, base64) used to sign portable decision receipts. Without it the
    # service generates an ephemeral key and says so: receipts will not verify after a restart.
    receipt_signing_seed: SecretStr | None = None
    receipt_verify_url: str = "/.well-known/artha-receipt-key.json"
    case_retention_days: int = 30

    web_origin: str | None = None
    supported_languages: list[str] = ["en", "hi", "id"]

    @property
    def ai_enabled(self) -> bool:
        return self.llm_mode != "offline"


@lru_cache
def get_settings() -> Settings:
    return Settings()
