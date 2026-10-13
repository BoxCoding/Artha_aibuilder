"""Where a customer is in their application, and what they need to do next.

Deterministic and borrower-safe by construction: it reads only status, documents, the decision and
the key fact statement, never scores, signals, features or pipeline step details, so nothing it
returns can reveal an internal finding. The video guide speaks from this and nothing else.
"""
from __future__ import annotations

from typing import Any

from app.domain.models import Case, CaseStatus, Outcome

# Order matters: it drives the progress bar.
STEPS = ("details", "statements", "submit", "processing", "review", "decided", "accept_terms", "done")
DECIDED = {CaseStatus.APPROVED, CaseStatus.APPROVED_WITH_CONDITIONS, CaseStatus.DECLINED, CaseStatus.REFERRED}


def journey(case: Case | None, max_documents: int = 3) -> dict[str, Any]:
    """The customer's current step, what is still missing, and the action that moves them on."""
    if case is None:
        return _state("details", missing=["details", "consent"], action="fill_form")
    documents = len(case.documents)
    common = {"documents": documents, "max_documents": max_documents, "business_name": case.business_name}

    if case.status == CaseStatus.DRAFT:
        if documents == 0:
            return _state("statements", missing=["statements"], action="upload", **common)
        return _state("submit", missing=[], action="submit", **common)
    if case.status == CaseStatus.PROCESSING:
        return _state("processing", missing=[], action="wait", **common)
    if case.status == CaseStatus.NEEDS_MANUAL_PROCESSING:
        # The stored failure_reason is already written for the customer by the pipeline.
        return _state("submit", missing=[], action="submit", attention=case.failure_reason, **common)
    if case.status in (CaseStatus.READY_FOR_REVIEW, CaseStatus.PENDING_SECOND_APPROVAL) or not case.decision:
        return _state("review", missing=[], action="wait", **common)

    approved = case.decision.final_outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS)
    if approved and case.kfs and not case.kfs_accepted_at:
        return _state("accept_terms", missing=["terms_acceptance"], action="view_decision", outcome="approved", **common)
    if approved:
        return _state("done", missing=[], action="view_decision", outcome="approved", **common)
    outcome = "declined" if case.status == CaseStatus.DECLINED else "referred"
    return _state("decided", missing=[], action="view_decision", outcome=outcome, **common)


def _state(step: str, *, missing: list[str], action: str, **extra: Any) -> dict[str, Any]:
    index = STEPS.index(step)
    return {"step": step, "step_number": index + 1, "total_steps": len(STEPS),
            "progress": round(100 * index / (len(STEPS) - 1)), "missing": missing, "action": action,
            **{k: v for k, v in extra.items() if v is not None}}
