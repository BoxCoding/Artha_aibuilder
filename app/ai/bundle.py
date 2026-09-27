"""Build the evidence bundle given to the memo writer and critic.

The bundle contains only masked, redacted, pre-formatted data. `allowed_numbers` is the complete
set of figures a memo may quote; the deterministic critic enforces it.
"""
from __future__ import annotations

from typing import Any

from app.domain.formatting import inr, pct
from app.domain.models import Case
from app.risk.engine import clause_text
from app.security.injection import REDACTED
from app.security.masking import mask_text

MAX_ROW_EVIDENCE = 40


def allowed_numbers(case: Case) -> dict[str, str]:
    f, s, p = case.features, case.score, case.policy
    assert s and p
    nums = {
        "requested_amount": f"₹{inr(case.requested_amount)}",
        "tenure_months": f"{case.tenure_months} months",
        "statement_span_months": f"{int(f['statement_span_months'])} months",
        "median_monthly_inflow": f"₹{inr(f['median_monthly_inflow'])}",
        "avg_daily_balance": f"₹{inr(f['avg_daily_balance'])}",
        "abb_to_inflow": pct(f["abb_to_inflow"]),
        "inflow_cv": pct(f["inflow_cv"]),
        "inflow_trend": pct(f["inflow_trend"]),
        "cash_deposit_share": pct(f["cash_deposit_share"]),
        "digital_share": pct(f["digital_share"]),
        "nach_returns_3m": str(int(f["nach_returns_3m"])),
        "nach_returns_6m": str(int(f["nach_returns_6m"])),
        "existing_emi_monthly": f"₹{inr(f['existing_emi_monthly'])}",
        "obligation_ratio": pct(f["obligation_ratio"]),
        "monthly_surplus": f"₹{inr(f['monthly_surplus'])}",
        "low_balance_days_share": pct(f["low_balance_days_share"]),
        "pd": pct(s.pd, 1),
        "band": s.band,
        "recommended_amount": f"₹{inr(p.recommended_amount)}",
        "max_emi_capacity": f"₹{inr(p.max_emi_capacity)}",
        "indicative_annual_rate": pct(float(p.indicative_annual_rate)) if p.indicative_annual_rate else "n/a",
        "policy_tenure": f"{p.tenure_months} months",
    }
    return nums


def build_bundle(case: Case, critic_issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    assert case.score and case.policy
    referenced = {e for sig in case.signals for e in sig.evidence_ids}
    catalogue: dict[str, str] = {}
    for ev in case.evidence.values():
        if ev.kind == "TXN_ROW" and ev.evidence_id not in referenced:
            continue
        description = ev.description
        if ev.kind == "TXN_ROW":
            txn = next((t for t in case.transactions if t.evidence_id == ev.evidence_id), None)
            if txn:
                narration = REDACTED if txn.redacted else mask_text(txn.narration)
                description += (f": {narration}; withdrawal {inr(txn.debit, 2)}; deposit {inr(txn.credit, 2)}; "
                                f"balance {inr(txn.balance, 2)}")
        catalogue[ev.evidence_id] = description
        if len(catalogue) > MAX_ROW_EVIDENCE + 12:
            break
    bundle: dict[str, Any] = {
        "applicant": {"business_name": case.business_name, "jurisdiction": case.jurisdiction,
                      "requested_amount": f"₹{inr(case.requested_amount)}", "tenure_months": case.tenure_months,
                      "document_count": len(case.documents)},
        "features": case.features,
        "signals": [{"code": s.code, "category": s.category, "severity": s.severity, "detector": s.detector,
                     "title": s.title, "explanation": s.explanation, "evidence_ids": s.evidence_ids}
                    for s in case.signals],
        "score": {"band": case.score.band, "pd": case.score.pd,
                  "reasons": [r.model_dump() for r in case.score.reasons],
                  "strengths": [r.model_dump() for r in case.score.strengths]},
        "policy": {"outcome": case.policy.outcome.value, "reason_codes": case.policy.reason_codes,
                   "triggered_clauses": case.policy.triggered_rules, "conditions": case.policy.conditions},
        "policy_clauses": {cid: clause_text(cid, case.jurisdiction) for cid in case.policy.triggered_rules},
        "evidence_catalogue": catalogue,
        "allowed_numbers": allowed_numbers(case),
    }
    if critic_issues:
        bundle["critic_issues"] = critic_issues
    return bundle
