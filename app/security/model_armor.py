"""Optional Model Armor screening (managed prompt-injection, jailbreak and sensitive-data filters).

Enabled only when MODEL_ARMOR_TEMPLATE is configured. Fails closed for user-facing chat
(the message is refused) and fails open-with-flag for document text (the local heuristic
layer still runs and the event is logged), so an outage cannot silently approve anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings

log = logging.getLogger("artha.model_armor")


@dataclass(frozen=True)
class ArmorVerdict:
    checked: bool
    blocked: bool
    detail: str


class ModelArmorGuard:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None
        if self._settings.model_armor_template:
            from google.api_core.client_options import ClientOptions
            from google.cloud import modelarmor_v1

            endpoint = f"modelarmor.{self._settings.model_armor_location}.rep.googleapis.com"
            self._client = modelarmor_v1.ModelArmorClient(client_options=ClientOptions(api_endpoint=endpoint))
            self._types = modelarmor_v1

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def screen_prompt(self, text: str) -> ArmorVerdict:
        if not self._client:
            return ArmorVerdict(False, False, "model_armor_not_configured")
        try:
            request = self._types.SanitizeUserPromptRequest(
                name=self._settings.model_armor_template,
                user_prompt_data=self._types.DataItem(text=text[:20000]),
            )
            response = self._client.sanitize_user_prompt(request=request)
            matched = response.sanitization_result.filter_match_state == self._types.FilterMatchState.MATCH_FOUND
            return ArmorVerdict(True, matched, "match_found" if matched else "no_match")
        except Exception as exc:  # noqa: BLE001 - external service; never crash the pipeline
            log.warning("model_armor_prompt_check_failed", extra={"error_type": type(exc).__name__})
            return ArmorVerdict(False, False, "model_armor_error")

    def screen_response(self, text: str) -> ArmorVerdict:
        if not self._client:
            return ArmorVerdict(False, False, "model_armor_not_configured")
        try:
            request = self._types.SanitizeModelResponseRequest(
                name=self._settings.model_armor_template,
                model_response_data=self._types.DataItem(text=text[:20000]),
            )
            response = self._client.sanitize_model_response(request=request)
            matched = response.sanitization_result.filter_match_state == self._types.FilterMatchState.MATCH_FOUND
            return ArmorVerdict(True, matched, "match_found" if matched else "no_match")
        except Exception as exc:  # noqa: BLE001
            log.warning("model_armor_response_check_failed", extra={"error_type": type(exc).__name__})
            return ArmorVerdict(False, False, "model_armor_error")
