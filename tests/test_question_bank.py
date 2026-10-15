"""Every question the video guide suggests is answered as intended, in every language."""
from __future__ import annotations

import pytest

from app.ai.guide import route
from app.ai.question_bank import LANGS, load, suggestions
from app.cashflow.categorise import apply_rules
from app.domain.kyc import ApplicantProfile
from app.domain.models import Case, Role
from app.domain.store import get_store
from app.risk.outcomes import build_training_case
from app.synthetic.generator import persona_statement
from tests.fixtures import APPLICANT
from tests.test_intake_and_roles import customer, new_case, staff, wait_until_processed

STATE = {"step": "review", "action": "wait", "documents": 1, "max_documents": 3}
CASES = [(g["tag"], g["needs"], q, lang) for g in load() for q in g["questions"] for lang in LANGS]


@pytest.fixture(scope="module")
def read_statement() -> Case:
    case = build_training_case(persona_statement("kiran"), 0)  # has EMIs, bounces, cash and UPI
    apply_rules(case.transactions)
    case.applicant = ApplicantProfile(**APPLICANT)
    return case


@pytest.fixture(scope="module")
def decided() -> Case:
    owner = customer()
    case_id = new_case(owner)
    owner.post(f"/api/cases/{case_id}/sample", json={"persona": "meena"})
    owner.post(f"/api/cases/{case_id}/submit")
    wait_until_processed(owner, case_id)
    officer = staff(Role.UNDERWRITER)
    policy = officer.get(f"/api/cases/{case_id}").json()["policy"]
    officer.post(f"/api/cases/{case_id}/decision", json={
        "outcome": policy["outcome"], "approved_amount": int(float(policy["recommended_amount"])) or None,
        "reason": "Agreed with the recommendation."})
    for lang in LANGS:
        owner.get(f"/api/cases/{case_id}/explanation", params={"language": lang})
    return get_store().get(case_id)


def test_every_question_has_all_languages_and_an_expectation() -> None:
    for group in load():
        assert set(group["label"]) == set(LANGS) and group["needs"] in ("always", "statement", "decision")
        assert any(q.get("featured") for q in group["questions"]), group["tag"]
        for q in group["questions"]:
            assert all(q.get(lang) for lang in LANGS) and q.get("expect"), q


@pytest.mark.parametrize("tag,needs,question,lang", CASES,
                         ids=[f"{t}-{lang}-{q['en'][:30]}" for t, _, q, lang in CASES])
def test_suggested_question_gets_the_intended_answer(tag: str, needs: str, question: dict, lang: str,
                                                     read_statement: Case, decided: Case) -> None:
    case = decided if needs == "decision" else read_statement
    reply, kind, _ = route(STATE, question[lang], lang, case)
    expected = question["expect"]
    assert kind == expected, f"{question[lang]!r} -> {kind}: {reply[:120]}"
    assert kind != "unclear"


def test_groups_appear_only_when_they_can_be_answered(read_statement: Case, decided: Case) -> None:
    before = {g["tag"] for g in suggestions(None, "en")}
    assert before == {"next", "me", "help"}
    assert {"income", "spending", "loans", "balance"} <= {g["tag"] for g in suggestions(read_statement, "en")}
    assert "decision" not in {g["tag"] for g in suggestions(read_statement, "en")}
    assert "decision" in {g["tag"] for g in suggestions(decided, "en")}
    hindi = suggestions(read_statement, "hi")
    assert hindi[0]["label"] == "आगे क्या" and hindi[0]["questions"][0]["text"] == "अब मुझे क्या करना है?"


def test_questions_endpoint_is_scoped_to_the_customers_own_case() -> None:
    owner = customer()
    case_id = new_case(owner)
    body = owner.get("/api/guide/questions", params={"case_id": case_id, "language": "id"}).json()
    assert [g["tag"] for g in body["groups"]] == ["next", "me", "help"]  # statement not read yet
    assert customer().get("/api/guide/questions", params={"case_id": case_id}).status_code == 404
    assert staff(Role.UNDERWRITER).get("/api/guide/questions").status_code == 403
