"""Single entry point for every Gemini call.

Responsibilities: client construction for Agent Platform (Vertex) or the Gemini API, versioned
prompt loading, structured output validation, timeouts, bounded retries with jitter, per-case
token budgets, and usage accounting. Business code never talks to the SDK directly.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings

log = logging.getLogger("artha.llm")
PROMPT_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Raised when AI mode is off, the budget is exhausted, or the model keeps failing."""


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    text: str


@lru_cache
def load_prompt(prompt_id: str) -> Prompt:
    raw = (PROMPT_DIR / f"{prompt_id}.md").read_text(encoding="utf-8")
    _, front, body = raw.split("---", 2)
    meta = dict(line.split(":", 1) for line in front.strip().splitlines())
    return Prompt(id=meta["id"].strip(), version=meta["version"].strip(), text=body.strip())


@dataclass
class UsageMeter:
    budget: int
    used: int = 0
    calls: list[dict[str, str | int]] = field(default_factory=list)

    def charge(self, prompt: Prompt, model: str, tokens: int, ms: int) -> None:
        self.used += tokens
        self.calls.append({"prompt": f"{prompt.id}.v{prompt.version}", "model": model, "tokens": tokens, "ms": ms})


class LLMGateway:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = None
        if self.settings.llm_mode == "vertex":
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                vertexai=True, project=self.settings.google_cloud_project, location=self.settings.google_cloud_location,
                http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_seconds * 1000)))
        elif self.settings.llm_mode == "api_key" and self.settings.gemini_api_key:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self.settings.gemini_api_key.get_secret_value(),
                http_options=types.HttpOptions(timeout=int(self.settings.llm_timeout_seconds * 1000)))

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def structured(self, *, prompt_id: str, schema: type[T], contents: list[object], meter: UsageMeter,
                         model: str | None = None, thinking: str | None = None, attempts: int = 2) -> T:
        """Call Gemini with a JSON response schema and return a validated instance of `schema`."""
        if not self._client:
            raise LLMUnavailable("AI mode is offline")
        if meter.used >= meter.budget:
            raise LLMUnavailable("token budget exhausted for this case")
        from google.genai import types

        prompt = load_prompt(prompt_id)
        model_id = model or self.settings.model_flash
        config = types.GenerateContentConfig(
            system_instruction=prompt.text,
            response_mime_type="application/json",
            response_schema=schema,
            # Sampling parameters (temperature/top_p/top_k) are deliberately not set: deprecated for
            # current Gemini models. Reasoning depth is controlled with the thinking level instead.
            thinking_config=types.ThinkingConfig(thinking_level=thinking) if thinking else None,
        )
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = await self._client.aio.models.generate_content(model=model_id, contents=contents,
                                                                          config=config)
                ms = int((time.monotonic() - started) * 1000)
                tokens = int(getattr(response.usage_metadata, "total_token_count", 0) or 0)
                meter.charge(prompt, model_id, tokens, ms)
                return schema.model_validate_json(response.text or "")
            except ValidationError as exc:
                last_error = exc
                log.warning("llm_schema_invalid", extra={"prompt": prompt.id, "attempt": attempt})
            except Exception as exc:  # noqa: BLE001 - SDK raises many transport-level types
                last_error = exc
                log.warning("llm_call_failed", extra={"prompt": prompt.id, "attempt": attempt,
                                                      "error_type": type(exc).__name__})
            await asyncio.sleep(min(8.0, 0.8 * 2 ** attempt + random.random()))
        raise LLMUnavailable(f"{prompt.id} failed after {attempts} attempts") from last_error


@lru_cache
def get_gateway() -> LLMGateway:
    return LLMGateway()
