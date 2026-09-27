"""Read-only tools exposed to the underwriter co-pilot agent.

Security invariant, unchanged from the rest of the system: **no agent holds a state-changing
tool.** Every function here reads one case and returns data. Nothing writes, nothing calls out,
nothing crosses a case boundary — the toolbox is constructed around a single case object, so a
question about another applicant cannot be answered even if the model asks for it.

Each tool returns a plain dict with an `evidence_ids` list wherever it makes a factual claim, so
the co-pilot's answers stay citable in exactly the way the credit memo is.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.cashflow.categorise import CATEGORIES
from app.domain.formatting import inr
from app.domain.models import Case
from app.risk.engine import apply_policy, load_policy, score

MAX_ROWS = 12


class CaseToolbox:
    """Builds tool callables bound to one case. The bound case is never a parameter, so the model
    cannot redirect a tool at a different applicant."""

    def __init__(self, case: Case) -> None:
        self._case = case
        self.calls: list[dict[str, Any]] = []

    def _record(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"tool": name, "args": {k: v for k, v in args.items() if v not in ("", 0, 0.0)},
                           "rows": result.get("count", 1),
                           "evidence_ids": list(result.get("evidence_ids", []))})
        return result

    @property
    def evidence_seen(self) -> list[str]:
        """Evidence IDs returned by every tool call so far, in order, deduplicated."""
        seen: list[str] = []
        for call in self.calls:
            for evidence_id in call.get("evidence_ids", []):
                if evidence_id not in seen:
                    seen.append(evidence_id)
        return seen

    # ---------------------------------------------------------------- tools
    def find_transactions(self, category: str = "", direction: str = "", min_amount: float = 0.0,
                          month: str = "", limit: int = MAX_ROWS) -> dict[str, Any]:
        """Find transactions in this applicant's statement.

        Args:
            category: one of the known categories, or empty for any.
            direction: "credit", "debit", or empty for any.
            min_amount: only transactions of at least this value.
            month: "YYYY-MM" to restrict to one month, or empty for the whole period.
            limit: maximum rows to return.
        """
        rows = self._case.transactions
        if category:
            rows = [t for t in rows if t.category == category]
        if direction == "credit":
            rows = [t for t in rows if t.credit > 0]
        elif direction == "debit":
            rows = [t for t in rows if t.debit > 0]
        if min_amount:
            threshold = Decimal(str(min_amount))
            rows = [t for t in rows if max(t.credit, t.debit) >= threshold]
        if month:
            rows = [t for t in rows if f"{t.txn_date.year}-{t.txn_date.month:02d}" == month]
        rows = sorted(rows, key=lambda t: max(t.credit, t.debit), reverse=True)[:max(1, min(limit, MAX_ROWS))]
        result = {
            "count": len(rows),
            "transactions": [{
                "date": t.txn_date.isoformat(), "narration": "[redacted]" if t.redacted else t.narration,
                "credit": str(t.credit), "debit": str(t.debit), "balance": str(t.balance),
                "category": t.category, "page": t.page, "evidence_id": t.evidence_id,
            } for t in rows],
            "evidence_ids": [t.evidence_id for t in rows],
        }
        return self._record("find_transactions", {"category": category, "direction": direction,
                                                  "min_amount": min_amount, "month": month}, result)

    def get_feature(self, name: str) -> dict[str, Any]:
        """Look up one computed cash-flow feature for this applicant.

        Args:
            name: the feature name, for example "median_monthly_inflow" or "nach_returns_3m".
        """
        if name not in self._case.features:
            result: dict[str, Any] = {"error": "unknown_feature", "available": sorted(self._case.features)}
        else:
            result = {"name": name, "value": self._case.features[name],
                      "evidence_ids": [e for e in ("ev-computed-inflow", "ev-computed-balance")
                                       if e in self._case.evidence]}
        return self._record("get_feature", {"name": name}, result)

    def list_findings(self, severity: str = "") -> dict[str, Any]:
        """List the integrity and risk findings raised on this applicant.

        Args:
            severity: "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", or empty for all.
        """
        signals = self._case.signals
        if severity:
            signals = [s for s in signals if s.severity == severity.upper()]
        result = {
            "count": len(signals),
            "findings": [{"code": s.code, "severity": s.severity, "category": s.category, "title": s.title,
                          "explanation": s.explanation, "corroborated": s.corroborated,
                          "detector": s.detector, "evidence_ids": s.evidence_ids} for s in signals],
            "evidence_ids": [e for s in signals for e in s.evidence_ids][:MAX_ROWS],
        }
        return self._record("list_findings", {"severity": severity}, result)

    def get_monthly_summary(self) -> dict[str, Any]:
        """Month-by-month inflow, outflow, average balance and returned instalments."""
        result = {"count": len(self._case.monthly), "months": self._case.monthly,
                  "evidence_ids": [e for e in ("ev-computed-inflow",) if e in self._case.evidence]}
        return self._record("get_monthly_summary", {}, result)

    def test_scenario(self, amount: int, tenure_months: int) -> dict[str, Any]:
        """Re-run the policy engine on a hypothetical structure. This changes nothing; it reports
        what the engine would decide for that amount and tenure.

        Args:
            amount: proposed principal in rupees.
            tenure_months: proposed tenure, one of 12, 18, 24 or 36.
        """
        case = self._case
        if not case.score:
            result: dict[str, Any] = {"error": "case_not_scored"}
        else:
            outcome = apply_policy(case.features, case.signals, case.score, Decimal(int(amount)),
                                   int(tenure_months), case.jurisdiction)
            result = {"requested": inr(Decimal(int(amount))), "tenure_months": outcome.tenure_months,
                      "outcome": outcome.outcome.value, "approved_amount": inr(outcome.recommended_amount),
                      "reason_codes": outcome.reason_codes, "conditions": outcome.conditions,
                      "triggered_clauses": outcome.triggered_rules}
        return self._record("test_scenario", {"amount": amount, "tenure_months": tenure_months}, result)

    # ---------------------------------------------------------------- wiring
    def as_functions(self) -> list[Callable[..., Any]]:
        return [self.find_transactions, self.get_feature, self.list_findings, self.get_monthly_summary,
                self.test_scenario]

    def as_adk_tools(self) -> list[Any]:
        from google.adk.tools import FunctionTool

        return [FunctionTool(func) for func in self.as_functions()]


def known_categories() -> tuple[str, ...]:
    return CATEGORIES


def policy_limits(jurisdiction: str = "IN") -> dict[str, Any]:
    return dict(load_policy(jurisdiction)["sizing"])


__all__ = ["CaseToolbox", "known_categories", "policy_limits", "score"]
