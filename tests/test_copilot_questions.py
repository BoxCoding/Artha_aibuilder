"""Every question the co-pilot suggests to a credit officer is answered by the intended route."""
from __future__ import annotations

import asyncio

import pytest

from app.ai.copilot import ask
from app.ai.copilot_questions import load, suggestions
from app.ai.gateway import get_gateway
from app.domain.models import Case, Role
from app.domain.store import get_store
from tests.test_intake_and_roles import customer, new_case, staff, wait_until_processed

QUESTIONS = [(g["tag"], q["q"], q["expect"]) for g in load() for q in g["questions"]]


def processed(persona: str) -> Case:
    owner = customer()
    case_id = new_case(owner)
    owner.post(f"/api/cases/{case_id}/sample", json={"persona": persona})
    owner.post(f"/api/cases/{case_id}/submit")
    wait_until_processed(owner, case_id)
    return get_store().get(case_id)


@pytest.fixture(scope="module")
def cases() -> dict[str, Case]:
    # An approvable case, a declined one with bounces and a name mismatch, and an edited statement.
    return {persona: processed(persona) for persona in ("meena", "kiran", "rapid")}


def test_every_group_has_featured_questions_and_expectations() -> None:
    for group in load():
        assert group["label"] and any(q.get("featured") for q in group["questions"]), group["tag"]
        assert all(q.get("q") and q.get("expect") for q in group["questions"])


@pytest.mark.parametrize("persona", ["meena", "kiran", "rapid"])
@pytest.mark.parametrize("tag,question,expect", QUESTIONS, ids=[f"{t}-{q[:40]}" for t, q, _ in QUESTIONS])
def test_suggested_question_is_answered_by_the_intended_route(cases: dict[str, Case], persona: str, tag: str,
                                                              question: str, expect: str) -> None:
    answer = asyncio.run(ask(cases[persona], question, get_gateway()))
    assert answer.kind == expect, f"{question!r} on {persona} -> {answer.kind}: {answer.text[:140]}"
    assert answer.text and "has not been scored" not in answer.text


def test_officer_answers_read_the_right_facts(cases: dict[str, Case]) -> None:
    gw = get_gateway()
    kiran, rapid = cases["kiran"], cases["rapid"]
    assert asyncio.run(ask(kiran, "Is the statement in the applicant's name?", gw)).text.startswith("No.")
    assert "no room for a new EMI" in asyncio.run(ask(kiran, "What EMI can they afford?", gw)).text
    editing = asyncio.run(ask(rapid, "Any signs of editing?", gw))
    assert "FR-" in editing.text and editing.evidence_ids
    assert "POS-" not in asyncio.run(ask(cases["meena"], "Any red flags?", gw)).text


def test_questions_endpoint_is_for_credit_staff_only() -> None:
    groups = staff(Role.UNDERWRITER).get("/api/copilot/questions").json()["groups"]
    assert [g["tag"] for g in groups] == [g["tag"] for g in suggestions()]
    assert customer().get("/api/copilot/questions").status_code == 403
