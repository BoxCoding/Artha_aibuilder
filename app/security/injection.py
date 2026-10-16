"""Heuristic prompt-injection detection for untrusted document text and user messages.

This is a local, always-on first layer. When a Model Armor template is configured,
app.security.model_armor adds a managed second layer. Neither layer is trusted alone:
agents additionally have no state-changing tools, and outputs are schema-validated.
"""
from __future__ import annotations

import re
import unicodedata

REDACTED = "[REDACTED_UNTRUSTED_CONTENT]"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx))
    for name, rx in (
        ("ignore_instructions", r"\b(ignore|disregard|forget|skip)\b(\s+\w+){0,3}\s+(rules?|instructions?|prompts?|polic(y|ies)|checks?)\b"),
        ("approve_directive", r"\b(approve|sanction|pass|accept)\b(\s+\w+){0,2}\s+(loan|applicant|application|case|file)\b"),
        ("role_override", r"\b(you are now|act as|pretend to be|new instructions)\b"),
        ("system_reference", r"\b(system|developer)\s+(prompt|message|note|instruction)s?\b"),
        ("model_addressing", r"\b(ai|llm|model|assistant|gemini|chatbot)\s+(note|notice|instruction|message)\b|\bnote to (the )?(ai|llm|model)\b"),
        ("policy_bypass", r"\b(override|bypass|disable|turn off)\b(\s+\w+){0,2}\s+(policy|rules?|checks?|guardrails?|filters?)\b"),
        ("score_manipulation", r"\b(set|make|mark)\b(\s+\w+){0,3}\s+(score|risk|band|pd)\s+(to|as)\b"),
    )
)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def detect(text: str) -> list[str]:
    """Return names of matched injection patterns (empty list when clean)."""
    norm = normalise(text)
    return [name for name, rx in _PATTERNS if rx.search(norm)]
