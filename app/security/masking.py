"""Masking of personal identifiers before text reaches logs or model prompts."""
from __future__ import annotations

import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[ID_12]"),
    (re.compile(r"(\+91[\s-]?)?\b[6-9]\d{9}\b"), "[PHONE]"),
)
_LONG_DIGITS = re.compile(r"\b\d{9,18}\b")


def mask_text(text: str) -> str:
    for pattern, token in _RULES:
        text = pattern.sub(token, text)
    return _LONG_DIGITS.sub(lambda m: "X" * (len(m.group()) - 4) + m.group()[-4:], text)
