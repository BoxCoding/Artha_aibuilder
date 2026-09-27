"""Schemas exchanged with Gemini. Kept flat and simple so they map cleanly to response schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CategoryName = Literal[
    "upi_credit", "neft_imps_credit", "card_settlement", "cash_deposit", "loan_disbursal", "self_transfer",
    "nach_return", "emi_debit", "return_charges", "supplier_payment", "rent", "utilities", "staff_salary",
    "cash_withdrawal", "bank_charges", "tax_payment", "other_credit", "other_debit",
]


class ExtractedTxn(BaseModel):
    page: int
    date: str | None
    narration: str | None
    reference: str | None
    debit: str | None
    credit: str | None
    balance: str | None
    box_2d: list[int] = Field(default_factory=list)


class StatementExtraction(BaseModel):
    bank_name: str | None
    account_holder: str | None
    account_last4: str | None
    period_from: str | None
    period_to: str | None
    opening_balance: str | None
    transactions: list[ExtractedTxn]
    suspicious_content: list[str] = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list)


class CategoryItem(BaseModel):
    txn_id: str
    category: CategoryName
    confidence: Literal["low", "medium", "high"]


class CategoryBatch(BaseModel):
    items: list[CategoryItem]


class VisualFinding(BaseModel):
    crop_id: str
    indicator: Literal["font_mismatch", "baseline_misalignment", "background_patch", "colour_mismatch",
                       "overlap_or_clipping", "other"]
    confidence: Literal["low", "medium", "high"]
    description: str


class VisualReport(BaseModel):
    findings: list[VisualFinding]


class DraftClaim(BaseModel):
    text: str
    evidence_ids: list[str]
    policy_clause_ids: list[str]


class DraftSection(BaseModel):
    key: Literal["summary", "cashflow", "repayment_capacity", "integrity", "risks_and_mitigants", "conditions"]
    title: str
    claims: list[DraftClaim]


class MemoDraft(BaseModel):
    sections: list[DraftSection]
    recommended_outcome: Literal["APPROVE", "APPROVE_WITH_CONDITIONS", "REFER", "REFER_INTEGRITY_REVIEW", "DECLINE"]
    open_questions: list[str]


class DraftIssue(BaseModel):
    claim_id: str | None
    issue_type: Literal["unsupported", "numeric_mismatch", "overstated_certainty", "protected_attribute",
                        "policy_inconsistency", "omitted_material_risk"]
    severity: Literal["blocker", "major", "minor"]
    explanation: str


class CriticDraft(BaseModel):
    issues: list[DraftIssue]
    verdict: Literal["PASS", "REVISE"]


class ExplanationDraft(BaseModel):
    outcome_sentence: str
    reasons: list[str]
    next_steps: list[str]
    key_terms: list[str]


class CoachDraft(BaseModel):
    reply: str
    offer_callback: bool
