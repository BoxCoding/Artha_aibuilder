"""Response shaping. Borrowers and staff get different projections of the same case, so internal
scores, signal codes and evidence never reach a borrower session."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.domain.formatting import emi, inr, pct
from app.domain.kyc import ApplicantProfile
from app.domain.models import Case, Explanation, Role
from app.risk.engine import band_rate, clause_text


def effective_rate(case: Case) -> Any:
    """The rate shown to an approved borrower, falling back to the band rate on an approval the
    policy engine did not itself recommend."""
    if case.policy and case.policy.indicative_annual_rate:
        return case.policy.indicative_annual_rate
    return band_rate(case.score.band, case.jurisdiction) if case.score else None


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    # Optional only so demo accounts (demo mode, never production) can sign in with an email alone.
    password: str | None = Field(default=None, max_length=256)


class RegisterRequest(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class CreateCaseRequest(BaseModel):
    applicant: ApplicantProfile
    business_name: str | None = Field(default=None, min_length=2, max_length=120)
    requested_amount: int = Field(ge=50_000, le=1_000_000)
    tenure_months: int = Field(ge=12, le=36)
    language: str = Field(default="en", max_length=5)
    consent: bool

    @field_validator("language")
    @classmethod
    def known_language(cls, v: str) -> str:
        return v if v in ("en", "hi", "id") else "en"


class DecisionRequest(BaseModel):
    outcome: str
    approved_amount: int | None = None
    reason: str | None = Field(default=None, max_length=600)


class CreateUserRequest(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(min_length=2, max_length=120)
    role: Role
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class CopilotRequest(BaseModel):
    question: str = Field(min_length=2, max_length=600)


class CoachRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class CallbackRequest(BaseModel):
    note: str = Field(default="", max_length=300)


def _checklist(case: Case) -> list[dict[str, Any]]:
    from app.compliance.checklist import evaluate

    return evaluate(case)


def _progress(case: Case) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for e in case.events:
        seen[e.step] = {"step": e.step, "label": e.label, "status": e.status, "duration_ms": e.duration_ms,
                        "detail": e.detail}
    return list(seen.values())


def borrower_view(case: Case, explanation: Explanation | None) -> dict[str, Any]:
    decision = case.decision
    payload: dict[str, Any] = {
        "case_id": case.case_id, "business_name": case.business_name, "status": case.status.value,
        "language": case.language, "requested_amount": inr(case.requested_amount),
        "tenure_months": case.tenure_months, "progress": _progress(case),
        "failure_reason": case.failure_reason, "decided": decision is not None,
        "applicant": case.applicant.model_dump(mode="json") if case.applicant else None,
        "documents": [{"doc_id": d.doc_id, "filename": d.filename} for d in case.documents],
        "kfs": case.kfs, "kfs_accepted_at": case.kfs_accepted_at,
        "recommendations": case.recommendations if decision else {},
    }
    if decision and explanation:
        amount = decision.approved_amount
        rate = effective_rate(case)
        payload["outcome"] = {
            "approved": decision.final_outcome.value.startswith("APPROVE"),
            "amount": inr(amount) if amount else None,
            "emi": inr(emi(amount, rate, case.policy.tenure_months)) if amount and rate and case.policy else None,
            "tenure_months": case.policy.tenure_months if case.policy else case.tenure_months,
            "rate": pct(float(rate)) if rate else None,
            "conditions": decision.conditions,
        }
        payload["explanation"] = explanation.model_dump()
    return payload


def staff_view(case: Case) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "case_id": case.case_id, "business_name": case.business_name, "status": case.status.value,
        "requested_amount": inr(case.requested_amount), "tenure_months": case.tenure_months,
        "created_at": case.created_at, "ai_mode": case.ai_mode, "token_usage": case.token_usage,
        "progress": _progress(case), "failure_reason": case.failure_reason,
        "channel": case.channel, "sourced_by": case.sourced_by,
        "applicant": case.applicant.model_dump(mode="json") if case.applicant else None,
        "applicant_warnings": case.applicant.warnings() if case.applicant else [],
        "compliance": _checklist(case), "kfs": case.kfs, "kfs_accepted_at": case.kfs_accepted_at,
        "compliance_clearances": case.compliance_clearances, "recommendations": case.recommendations,
        "documents": [{"doc_id": d.doc_id, "filename": d.filename, "pages": d.page_count, "parser": d.parser,
                       "sha256": d.sha256[:16], "bank": d.header.bank_name, "holder": d.header.account_holder,
                       "account_last4": d.header.account_last4, "period_from": d.header.period_from,
                       "period_to": d.header.period_to, "metadata": d.metadata,
                       "incremental_updates": d.incremental_updates, "decrypted": d.decrypted}
                      for d in case.documents],
        "signals": [s.model_dump() for s in sorted(
            case.signals, key=lambda s: (-"INFO LOW MEDIUM HIGH CRITICAL".split().index(s.severity), s.code))],
        "features": case.features, "monthly": case.monthly,
        "evidence": {k: v.model_dump() for k, v in case.evidence.items()},
        "transaction_count": len(case.transactions),
    }
    if case.score:
        payload["score"] = case.score.model_dump()
    if case.policy:
        payload["policy"] = case.policy.model_dump(mode="json") | {
            "clauses": {c: clause_text(c, case.jurisdiction) for c in case.policy.triggered_rules},
            "recommended_amount_display": inr(case.policy.recommended_amount),
            "max_emi_capacity_display": inr(case.policy.max_emi_capacity),
        }
    if case.model_challenge:
        payload["model_challenge"] = case.model_challenge
    if case.structuring:
        payload["structuring"] = case.structuring
    if case.memo:
        payload["memo"] = case.memo.model_dump()
    if case.decision:
        payload["decision"] = case.decision.model_dump(mode="json")
    if case.pending_decision:
        payload["pending_decision"] = case.pending_decision.model_dump(mode="json")
    payload["explanations"] = {k: v.model_dump() for k, v in case.explanations.items()}
    return payload


def case_view(case: Case, role: Role, explanation: Explanation | None = None) -> dict[str, Any]:
    return borrower_view(case, explanation) if role == Role.BORROWER else staff_view(case)
