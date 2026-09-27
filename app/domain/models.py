"""Domain entities. Money is always Decimal; LLM-facing schemas live in app/ai/schemas.py."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(StrEnum):
    BORROWER = "borrower"
    UNDERWRITER = "underwriter"
    RISK_ADMIN = "risk_admin"


class CaseStatus(StrEnum):
    DRAFT = "DRAFT"
    PROCESSING = "PROCESSING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NEEDS_MANUAL_PROCESSING = "NEEDS_MANUAL_PROCESSING"
    PENDING_SECOND_APPROVAL = "PENDING_SECOND_APPROVAL"
    APPROVED = "APPROVED"
    APPROVED_WITH_CONDITIONS = "APPROVED_WITH_CONDITIONS"
    DECLINED = "DECLINED"
    REFERRED = "REFERRED"


class Outcome(StrEnum):
    APPROVE = "APPROVE"
    APPROVE_WITH_CONDITIONS = "APPROVE_WITH_CONDITIONS"
    REFER = "REFER"
    REFER_INTEGRITY_REVIEW = "REFER_INTEGRITY_REVIEW"
    DECLINE = "DECLINE"


OUTCOME_TO_STATUS: dict[Outcome, CaseStatus] = {
    Outcome.APPROVE: CaseStatus.APPROVED,
    Outcome.APPROVE_WITH_CONDITIONS: CaseStatus.APPROVED_WITH_CONDITIONS,
    Outcome.REFER: CaseStatus.REFERRED,
    Outcome.REFER_INTEGRITY_REVIEW: CaseStatus.REFERRED,
    Outcome.DECLINE: CaseStatus.DECLINED,
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Strict):
    evidence_id: str
    doc_id: str
    page: int  # 1-based
    bbox: list[int] | None = None  # [ymin, xmin, ymax, xmax] normalised 0-1000
    kind: Literal["TXN_ROW", "HEADER", "METADATA", "CELL", "COMPUTED"]
    description: str
    derived_from: list[str] = Field(default_factory=list)


class Transaction(Strict):
    txn_id: str
    doc_id: str
    page: int
    row_index: int
    txn_date: date
    narration: str
    reference: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    balance: Decimal
    evidence_id: str
    category: str = "other"
    category_source: Literal["RULE", "LLM", "NONE"] = "NONE"
    amount_font: str | None = None
    amount_size: float | None = None
    amount_bbox: list[int] | None = None
    redacted: bool = False


class StatementHeader(Strict):
    bank_name: str | None = None
    account_holder: str | None = None
    account_last4: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    opening_balance: Decimal | None = None


class Document(Strict):
    doc_id: str
    filename: str
    sha256: str
    size_bytes: int
    page_count: int
    doc_type: str = "bank_statement"
    parser: Literal["template", "gemini", "none"] = "none"
    header: StatementHeader = Field(default_factory=StatementHeader)
    metadata: dict[str, str] = Field(default_factory=dict)
    incremental_updates: int = 0


class Signal(Strict):
    signal_id: str
    code: str
    category: Literal["FRAUD", "RISK", "POSITIVE"]
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
    confidence: float = Field(ge=0, le=1)
    detector: Literal["RULE", "LLM_VISUAL", "MODEL_ARMOR"]
    corroborated: bool
    evidence_ids: list[str] = Field(min_length=1)
    title: str
    explanation: str


class ReasonCode(Strict):
    code: str
    feature: str
    direction: Literal["increases_risk", "decreases_risk"]
    contribution: float
    value: float


class ScoreResult(Strict):
    scorecard_version: str
    pd: float
    band: Literal["A", "B", "C", "D", "E"]
    reasons: list[ReasonCode]
    strengths: list[ReasonCode]


class PolicyResult(Strict):
    policy_id: str
    policy_version: str
    outcome: Outcome
    triggered_rules: list[str]
    reason_codes: list[str]
    recommended_amount: Decimal
    max_emi_capacity: Decimal
    indicative_annual_rate: Decimal | None
    tenure_months: int
    conditions: list[str] = Field(default_factory=list)


class ImprovementStep(Strict):
    feature: str
    current: float
    target: float
    hint_key: str


class Claim(Strict):
    claim_id: str
    text: str = Field(max_length=700)
    evidence_ids: list[str] = Field(default_factory=list)
    policy_clause_ids: list[str] = Field(default_factory=list)


class MemoSection(Strict):
    key: str
    title: str
    claims: list[Claim]


class CriticIssue(Strict):
    claim_id: str | None
    issue_type: Literal[
        "unsupported", "numeric_mismatch", "overstated_certainty", "protected_attribute",
        "policy_inconsistency", "omitted_material_risk", "invalid_citation",
    ]
    severity: Literal["blocker", "major", "minor"]
    explanation: str
    source: Literal["deterministic", "llm"]


class CreditMemo(Strict):
    version: int
    author: Literal["gemini", "offline_composer"]
    sections: list[MemoSection]
    recommended_outcome: Outcome
    open_questions: list[str] = Field(default_factory=list)
    critic_issues: list[CriticIssue] = Field(default_factory=list)
    critic_verdict: Literal["PASS", "REVISE"] = "PASS"
    iterations: int = 1
    dropped_claims: list[str] = Field(default_factory=list)


class Decision(Strict):
    decision_id: str
    system_recommendation: Outcome
    final_outcome: Outcome
    approved_amount: Decimal | None
    conditions: list[str] = Field(default_factory=list)
    override: bool
    reason: str | None
    decided_by: str
    decided_at: datetime = Field(default_factory=utcnow)
    second_approver: str | None = None


class PipelineEvent(Strict):
    step: str
    label: str
    status: Literal["started", "completed", "skipped", "failed"]
    at: datetime = Field(default_factory=utcnow)
    duration_ms: int | None = None
    detail: dict[str, str | int | float] = Field(default_factory=dict)


class Explanation(Strict):
    language: str
    outcome_sentence: str
    reasons: list[str]
    next_steps: list[str]
    key_terms: list[str]
    contact: str
    integrity_review: bool
    author: Literal["gemini", "templates"]


class Case(Strict):
    case_id: str
    owner_uid: str
    business_name: str
    requested_amount: Decimal
    tenure_months: int
    language: str = "en"
    jurisdiction: str = "IN"
    status: CaseStatus = CaseStatus.DRAFT
    consent_given_at: datetime | None = None
    consent_notice_version: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    documents: list[Document] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    transactions: list[Transaction] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    features: dict[str, float] = Field(default_factory=dict)
    monthly: list[dict[str, str | float]] = Field(default_factory=list)
    score: ScoreResult | None = None
    policy: PolicyResult | None = None
    improvement_plan: list[ImprovementStep] = Field(default_factory=list)
    structuring: dict[str, Any] = Field(default_factory=dict)
    model_challenge: dict[str, Any] = Field(default_factory=dict)
    memo: CreditMemo | None = None
    decision: Decision | None = None
    pending_decision: Decision | None = None
    explanations: dict[str, Explanation] = Field(default_factory=dict)
    events: list[PipelineEvent] = Field(default_factory=list)
    ai_mode: str = "offline"
    token_usage: int = 0
    failure_reason: str | None = None


class AuditEvent(Strict):
    seq: int
    at: datetime
    actor: str
    role: str
    action: str
    resource: str
    detail_hash: str
    prev_hash: str
    event_hash: str
