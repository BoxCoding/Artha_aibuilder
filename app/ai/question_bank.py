"""Suggested questions for the video guide, grouped by tag (data in question_bank.yaml)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.domain.models import Case

PATH = Path(__file__).with_name("question_bank.yaml")
LANGS = ("en", "hi", "id")


@lru_cache
def load() -> list[dict[str, Any]]:
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))["groups"]


def available(item: dict[str, Any], case: Case | None) -> bool:
    """Whether a group or question can be answered yet for this application."""
    needs = item.get("needs", "always")
    if needs == "case":
        return case is not None
    if needs == "statement":
        return bool(case and case.transactions)
    if needs == "decision":
        return bool(case and case.decision)
    return True


def suggestions(case: Case | None, lang: str) -> list[dict[str, Any]]:
    """The groups that make sense for this application right now, in one language."""
    lang = lang if lang in LANGS else "en"
    groups = []
    for g in load():
        if not available(g, case):
            continue
        questions = [{"text": q[lang], "featured": bool(q.get("featured"))} for q in g["questions"] if available(q, case)]
        if questions:
            if not any(q["featured"] for q in questions):
                questions[0]["featured"] = True
            groups.append({"tag": g["tag"], "label": g["label"][lang], "questions": questions})
    return groups
