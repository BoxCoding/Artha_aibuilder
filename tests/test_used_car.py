"""Used car loans: salaried or self-employed is read from the bank data, the car caps the loan (LTV and age),
and approval waits for an officer's valuation. All data fictional."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.cashflow.categorise import apply_rules
from app.cashflow.income_type import detect_income_type
from app.documents.ingest import attach_parsed_statement
from app.documents.intake import read_deterministic
from app.domain.models import Case, Role, ScoreResult, Vehicle
from app.risk.engine import apply_policy
from app.synthetic.generator import persona_statement, render_pdf
from tests.fixtures import staff
from tests.test_coverage import applicant

CAR = {"make": "Maruti", "model": "Swift VXI", "manufacture_year": 2021, "registration_number": "MH12AB1234",
       "odometer_km": 42000, "seller": "dealer", "purchase_price": 600000}


def _reading(persona: str) -> dict:
    pdf = render_pdf(persona_statement(persona))
    parsed, _ = read_deterministic(pdf)
    case = Case(case_id="t", owner_uid="o", business_name="t", requested_amount=Decimal(1), tenure_months=12,
                product="used_car")
    attach_parsed_statement(case, "d1", "s.pdf", pdf, parsed, "template")
    apply_rules(case.transactions)
    return detect_income_type(case)


def test_salaried_and_self_employed_are_told_apart_from_the_statement() -> None:
    ravi = _reading("ravi")
    assert ravi["type"] == "salaried" and ravi["confidence"] >= 0.9 and ravi["employer"] == "Orbit Software"
    assert ravi["salary_months"] == 6 and 80000 < ravi["salary_monthly"] < 90000
    for persona in ("meena", "kiran", "rapid"):  # a regular customer of a business is not a salary
        reading = _reading(persona)
        assert reading["type"] == "self_employed" and reading["confidence"] >= 0.7, (persona, reading)


def _decide(age_years: float, value: int, requested: int = 500000, tenure: int = 60) -> object:
    features = {"statement_span_months": 6.0, "nach_returns_3m": 0.0, "assessed_monthly_income": 90000.0,
                "monthly_surplus": 40000.0, "existing_emi_monthly": 0.0, "avg_daily_balance": 50000.0}
    score = ScoreResult(scorecard_version="t", pd=0.02, band="A", reasons=[], strengths=[])
    return apply_policy(features, [], score, Decimal(requested), tenure, "IN_used_car",
                        {"vehicle_value": Decimal(value), "vehicle_age_years": age_years, "valued": True})


def test_the_car_caps_the_loan_and_the_tenure() -> None:
    young = _decide(3.0, 500000)  # 80% of 5,00,000
    assert young.outcome.value == "APPROVE" and young.recommended_amount == Decimal("400000")
    older = _decide(6.0, 500000)  # 70% for a car over five years old; 60 months would take it past ten years
    assert older.recommended_amount == Decimal("350000") and older.tenure_months == 48
    assert "POL-VEHICLE-AGE" in older.triggered_rules and "POL-LTV" in older.triggered_rules
    too_old = _decide(9.5, 500000)
    assert too_old.outcome.value == "DECLINE" and "RC-VEHICLE-TOO-OLD" in too_old.reason_codes


def test_vehicle_details_are_required_and_checked() -> None:
    officer = staff(Role.LOAN_OFFICER)
    base = {"applicant": applicant(), "product": "used_car", "requested_amount": 450000, "tenure_months": 48,
            "language": "en", "consent": True}
    assert officer.post("/api/cases", json=base).status_code == 422  # no car
    assert officer.post("/api/cases", json=base | {"vehicle": CAR | {"purchase_price": 300000}}).status_code == 422
    assert officer.post("/api/cases", json=base | {"vehicle": CAR | {"valuation": 900000}}).status_code == 422
    assert officer.post("/api/cases", json=base | {"vehicle": CAR, "tenure_months": 18}).status_code == 422
    assert officer.post("/api/cases", json=base | {"vehicle": CAR | {"registration_number": "12345"}}).status_code == 422
    assert Vehicle(**CAR | {"registration_number": "mh 12 ab 1234"}).registration_number == "MH12AB1234"


def test_salaried_used_car_loan_end_to_end() -> None:
    import time

    officer, credit = staff(Role.LOAN_OFFICER), staff(Role.UNDERWRITER)
    details = applicant(full_name="Ravi Kumar")
    details.pop("business_name", None)
    cid = officer.post("/api/cases", json={"applicant": details, "product": "used_car", "vehicle": CAR,
                                           "requested_amount": 450000, "tenure_months": 48, "language": "en",
                                           "consent": True}).json()["case_id"]
    pdf = render_pdf(persona_statement("ravi"))
    upload = officer.post(f"/api/cases/{cid}/documents", files={"file": ("ravi.pdf", pdf, "application/pdf")}).json()
    hint = upload["preview"]["income_hint"]
    assert hint["type"] == "salaried" and hint["employer"] == "Orbit Software"
    assert officer.post(f"/api/cases/{cid}/follow-up", json={"employer_name": "Orbit Software Pvt Ltd"}).status_code == 200
    assert officer.post(f"/api/cases/{cid}/submit").status_code == 200
    for _ in range(160):
        body = officer.get(f"/api/cases/{cid}").json()
        if body["status"] not in ("PROCESSING", "DRAFT"):
            break
        time.sleep(0.25)
    assert body["status"] == "READY_FOR_REVIEW", body.get("failure_reason")
    assert body["product"] == "used_car" and body["business_name"] == "Ravi Kumar · Maruti Swift VXI"
    profile = body["income_profile"]
    assert profile["current_type"] == "salaried" and profile["declared"]["employer_name"] == "Orbit Software Pvt Ltd"
    assert {"POL-LTV", "POL-VALUATION", "POL-HYPOTHECATION"} <= set(body["policy"]["triggered_rules"])
    assert "ev-computed-salary" in body["evidence"]

    # No approval before the car is valued; then the LTV cap on the lower of price and valuation binds.
    blocked = credit.post(f"/api/cases/{cid}/decision", json={"outcome": "APPROVE"})
    assert blocked.status_code == 409 and "valuation" in blocked.json()["detail"]
    valued = officer.post(f"/api/cases/{cid}/vehicle/valuation", json={"valuation": 520000}).json()
    assert valued["loan_cap"] == "360000"  # car over five years: 70% of the lower of 6,00,000 and 5,20,000, in 5,000 steps
    cap = Decimal(valued["loan_cap"])
    over = credit.post(f"/api/cases/{cid}/decision", json={"outcome": "APPROVE", "approved_amount": int(cap) + 5000,
                                                          "reason": "Customer asked for more"})
    assert over.status_code == 400 and "LTV" in over.json()["detail"]
    ok = credit.post(f"/api/cases/{cid}/decision", json={"outcome": "APPROVE", "approved_amount": int(cap),
                                                        "reason": "Within LTV after valuation"})
    assert ok.status_code == 200, ok.text
    kfs = credit.get(f"/api/cases/{cid}").json()["kfs"]
    assert "MH12AB1234" in kfs["security"] and "hypothecation" in kfs["loan_type"]


def test_an_officer_can_correct_the_income_type_with_a_reason() -> None:
    import time

    officer, credit = staff(Role.LOAN_OFFICER), staff(Role.UNDERWRITER)
    cid = officer.post("/api/cases", json={"applicant": applicant(), "product": "used_car", "vehicle": CAR,
                                           "requested_amount": 300000, "tenure_months": 36, "language": "en",
                                           "consent": True}).json()["case_id"]
    officer.post(f"/api/cases/{cid}/documents",
                 files={"file": ("s.pdf", render_pdf(persona_statement("meena")), "application/pdf")})
    officer.post(f"/api/cases/{cid}/submit")
    for _ in range(160):
        if officer.get(f"/api/cases/{cid}").json()["status"] not in ("PROCESSING", "DRAFT"):
            break
        time.sleep(0.25)
    assert credit.post(f"/api/cases/{cid}/income-type", json={"income_type": "salaried", "reason": "x"}).status_code == 422
    assert officer.post(f"/api/cases/{cid}/income-type",
                        json={"income_type": "salaried", "reason": "Payslips seen"}).status_code == 403
    r = credit.post(f"/api/cases/{cid}/income-type", json={"income_type": "salaried",
                                                            "reason": "Employer letter and payslips verified"})
    assert r.status_code == 200, r.text
    profile = credit.get(f"/api/cases/{cid}").json()["income_profile"]
    assert profile["type"] == "self_employed" and profile["current_type"] == "salaried"
    assert profile["override"]["reason"].startswith("Employer letter")


@pytest.mark.parametrize("product", ["working_capital"])
def test_working_capital_is_unchanged(product: str) -> None:
    officer = staff(Role.LOAN_OFFICER)
    details = applicant()
    details.pop("business_name", None)
    r = officer.post("/api/cases", json={"applicant": details, "requested_amount": 300000, "tenure_months": 24,
                                         "language": "en", "consent": True})
    assert r.status_code == 422  # a working-capital loan still needs the business
