"""Per-case compliance checklist: what a regulator or internal auditor would check, evaluated live.

Statuses: pass, attention (needs a person), pending (not yet due), fail, n/a. Approval is blocked by
`approval_blockers`; the rest informs the credit officer and the auditor report.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.domain.models import Case


def uncleared_compliance_hits(case: Case) -> list[str]:
    cleared = {c["code"] for c in case.compliance_clearances}
    return sorted({s.code for s in case.signals if s.category == "COMPLIANCE" and s.severity in ("HIGH", "CRITICAL")
                   and s.code not in cleared})


def approval_blockers(case: Case) -> list[str]:
    problems = []
    if case.applicant is None:
        problems.append("Customer KYC details are missing.")
    if not case.consent_given_at:
        problems.append("The customer's consent is not recorded.")
    hits = uncleared_compliance_hits(case)
    if hits:
        problems.append("Watchlist matches (" + ", ".join(hits) + ") must be cleared by compliance before approval.")
    return problems


def evaluate(case: Case) -> list[dict[str, Any]]:
    settings = get_settings()
    items: list[dict[str, Any]] = []

    def add(item_id: str, label: str, status: str, detail: str) -> None:
        items.append({"id": item_id, "label": label, "status": status, "detail": detail})

    a = case.applicant
    add("kyc", "Customer details captured and format-validated", "pass" if a else "fail",
        f"PAN {a.pan[:2]}XXX{a.pan[5:]} · verify with NSDL" if a else "No KYC details on the application")
    add("consent", "Consent recorded with the notice version", "pass" if case.consent_given_at else "fail",
        f"{case.consent_notice_version} at {case.consent_given_at:%d %b %Y %H:%M} UTC" if case.consent_given_at else "missing")
    months = case.features.get("statement_span_months")
    if months is None:
        add("history", "At least six months of statements", "pending", "Analysis not finished")
    else:
        add("history", "At least six months of statements", "pass" if months >= 6 else "attention", f"{months:.0f} months")
    if not case.score:
        add("aml", "AML watchlist and cash-reporting screening", "pending", "Runs with the analysis")
    else:
        hits = uncleared_compliance_hits(case)
        ctr = [s for s in case.signals if s.code == "AML-CTR-001"]
        cleared = [c["code"] for c in case.compliance_clearances]
        detail = "No watchlist match" if not hits else "Uncleared: " + ", ".join(hits)
        if cleared:
            detail += f" · cleared by compliance: {', '.join(cleared)}"
        if ctr:
            detail += f" · {len(ctr)} month(s) of reportable cash"
        add("aml", "AML watchlist and cash-reporting screening", "attention" if hits else "pass", detail)
    severe = [s.code for s in case.signals if s.category == "FRAUD" and s.severity in ("HIGH", "CRITICAL")]
    if case.score:
        add("integrity", "Document integrity reviewed", "attention" if severe and not case.decision else "pass",
            ("Open findings: " + ", ".join(sorted(set(severe)))) if severe else "No serious findings")
    d = case.decision
    add("human", "Decision recorded by a person", "pass" if d else "pending", f"by {d.decided_by}" if d else "Awaiting a credit officer")
    threshold = Decimal("500000")
    if d and d.approved_amount and d.approved_amount > threshold:
        add("four_eyes", "Second approver above ₹5,00,000", "pass" if d.second_approver else "fail",
            f"countersigned by {d.second_approver}" if d.second_approver else "missing")
    else:
        add("four_eyes", "Second approver above ₹5,00,000", "n/a", "Not required")
    if d and d.override:
        add("override", "Reason recorded for departing from the recommendation", "pass" if d.reason else "fail", d.reason or "missing")
    approved = bool(d and d.final_outcome.value.startswith("APPROVE"))
    if approved:
        add("kfs", "Key Fact Statement issued before sanction", "pass" if case.kfs else "fail",
            f"APR {case.kfs['apr_pct']}% · {case.kfs['kfs_sha256'][:12]}" if case.kfs else "missing")
        add("kfs_accept", "Customer accepted the Key Fact Statement (disbursal gate)",
            "pass" if case.kfs_accepted_at else "pending",
            f"accepted {case.kfs_accepted_at:%d %b %Y %H:%M} UTC" if case.kfs_accepted_at else "Awaiting the customer")
    else:
        add("kfs", "Key Fact Statement issued before sanction", "n/a", "Only for approvals")
    if d:
        add("explained", "Outcome explained in the customer's language", "pass" if case.language in case.explanations else "pending",
            f"language: {case.language}")
    retention = case.updated_at + timedelta(days=settings.case_retention_days)
    add("retention", "Retention and deletion scheduled", "pass", f"deleted after {retention:%d %b %Y} unless the customer asks sooner")
    return items
