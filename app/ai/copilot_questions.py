"""Suggested questions for the credit officer's co-pilot, grouped by tag (data in copilot_questions.yaml)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).with_name("copilot_questions.yaml")


@lru_cache
def load() -> list[dict[str, Any]]:
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))["groups"]


def suggestions() -> list[dict[str, Any]]:
    return [{"tag": g["tag"], "label": g["label"],
             "questions": [{"text": q["q"], "featured": bool(q.get("featured"))} for q in g["questions"]]}
            for g in load()]
