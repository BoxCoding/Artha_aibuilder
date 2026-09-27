"""Single entry point for every model call.

Providers:
  * vertex / api_key - Gemini (Vertex AI with the service account, or the Gemini API for development)
  * ollama           - a local model served by Ollama (e.g. Gemma), so no customer data leaves the
                       deployment. Structured output is enforced with Ollama's JSON-schema `format`.
  * offline          - no model; every AI step uses its deterministic fallback.

Responsibilities: client construction, versioned prompt loading, structured output validation,
timeouts, bounded retries with jitter, per-case token budgets and usage accounting. Business code
never talks to a provider SDK directly, and passes attachments as `Blob`s so it does not care which
provider is configured.
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


@dataclass(frozen=True)
class Blob:
    """A binary attachment (a PDF or an image) in a provider-neutral form."""
    data: bytes
    mime_type: str


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
        self._ollama: OllamaClient | None = None
        if self.settings.llm_mode == "ollama":
            self._ollama = OllamaClient(self.settings.ollama_url, timeout=self.settings.llm_timeout_seconds)
        elif self.settings.llm_mode == "vertex":
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
        return self._client is not None or self._ollama is not None

    @property
    def provider(self) -> str:
        """Label recorded as the author of AI-written artefacts."""
        return "ollama" if self._ollama is not None else "gemini"

    @property
    def supports_tools(self) -> bool:
        """Whether agentic tool use (the ADK co-pilot) is available. Small local models are not
        dependable tool callers, so the grounded tool router answers instead."""
        return self._client is not None

    @property
    def supports_vision(self) -> bool:
        return self._client is not None or bool(self._ollama and self.settings.ollama_vision_model)

    async def structured(self, *, prompt_id: str, schema: type[T], contents: list[object], meter: UsageMeter,
                         model: str | None = None, thinking: str | None = None, attempts: int = 2) -> T:
        """Call the configured model with a JSON response schema and return a validated `schema`."""
        if not self.enabled:
            raise LLMUnavailable("AI mode is offline")
        if meter.used >= meter.budget:
            raise LLMUnavailable("token budget exhausted for this case")
        prompt = load_prompt(prompt_id)
        if self._ollama is not None:
            return await self._ollama_structured(prompt, schema, contents, meter, model, attempts)
        from google.genai import types

        contents = [types.Part.from_bytes(data=c.data, mime_type=c.mime_type) if isinstance(c, Blob) else c
                    for c in contents]
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


    async def _ollama_structured(self, prompt: Prompt, schema: type[T], contents: list[object], meter: UsageMeter,
                                 model: str | None, attempts: int) -> T:
        assert self._ollama is not None
        text_parts: list[str] = []
        images: list[bytes] = []
        for item in contents:
            if isinstance(item, Blob):
                if item.mime_type == "application/pdf":
                    text_parts.append(pdf_as_text(item.data))
                elif item.mime_type.startswith("image/"):
                    images.append(item.data)
            else:
                text_parts.append(str(item))
        model_id = self.settings.ollama_model
        if images:
            if not self.settings.ollama_vision_model:
                raise LLMUnavailable("no vision model configured for images")
            model_id = self.settings.ollama_vision_model
        json_schema = inline_refs(schema.model_json_schema())
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                raw, tokens = await self._ollama.chat(model=model_id, system=prompt.text, user="\n\n".join(text_parts),
                                                      images=images, schema=json_schema,
                                                      num_ctx=self.settings.ollama_num_ctx)
                meter.charge(prompt, f"ollama:{model_id}", tokens, int((time.monotonic() - started) * 1000))
                return schema.model_validate_json(raw)
            except ValidationError as exc:
                last_error = exc
                log.warning("llm_schema_invalid", extra={"prompt": prompt.id, "attempt": attempt})
            except Exception as exc:  # noqa: BLE001 - connection refused, timeouts, model not pulled
                last_error = exc
                log.warning("llm_call_failed", extra={"prompt": prompt.id, "attempt": attempt,
                                                      "error_type": type(exc).__name__})
            await asyncio.sleep(min(4.0, 0.5 * 2 ** attempt + random.random()))
        raise LLMUnavailable(f"{prompt.id} failed after {attempts} attempts") from last_error


class OllamaClient:
    """Minimal async client for Ollama's /api/chat with JSON-schema constrained output."""

    def __init__(self, base_url: str, timeout: float = 90.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def chat(self, *, model: str, system: str, user: str, images: list[bytes], schema: dict,
                   num_ctx: int = 8192) -> tuple[str, int]:
        import base64

        import httpx

        message: dict[str, object] = {"role": "user", "content": user}
        if images:
            message["images"] = [base64.b64encode(i).decode("ascii") for i in images]
        body = {"model": model, "stream": False, "format": schema,
                "messages": [{"role": "system", "content": system}, message],
                "options": {"temperature": 0, "num_ctx": num_ctx}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/api/chat", json=body)
            response.raise_for_status()
            payload = response.json()
        tokens = int(payload.get("prompt_eval_count", 0) or 0) + int(payload.get("eval_count", 0) or 0)
        return payload.get("message", {}).get("content", ""), tokens

    async def ping(self, model: str) -> tuple[bool, str]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
            names = {m.get("name") for m in response.json().get("models", [])}
            if model not in names and f"{model}:latest" not in names:
                return False, f"model {model} is not pulled (run: ollama pull {model})"
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, f"ollama unreachable ({type(exc).__name__})"


def pdf_as_text(data: bytes, max_chars: int = 60_000) -> str:
    """Local models read statements as layout-preserving text, page by page."""
    import io

    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            pages.append(f"--- page {number} ---\n{page.extract_text(layout=True) or ''}")
    return "\n".join(pages)[:max_chars]


def inline_refs(schema: dict) -> dict:
    """Resolve $ref/$defs so the schema is self-contained (what grammar-constrained decoders expect)."""
    defs = schema.get("$defs", {})

    def resolve(node: object) -> object:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)  # type: ignore[return-value]


@lru_cache
def get_gateway() -> LLMGateway:
    return LLMGateway()
