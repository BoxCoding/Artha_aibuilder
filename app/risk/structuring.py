"""Deal structuring: can this application become approvable without bending policy?

A decline is cheap to produce and expensive for everyone. Before a case is turned away, this
module searches the structures a lender actually controls - principal, tenure, and the conditions
attached - and reports the ones the *unmodified* policy engine would approve.

It is deterministic and it cannot bypass a knockout: each candidate is put back through
`apply_policy`, so an integrity referral or a recent-returns decline stays exactly that. If no
structure works, it says so and names what is blocking, which is what the borrower needs to hear.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.formatting import emi, inr
from app.domain.models import Case, Outcome
from app.risk.engine import apply_policy, load_policy

APPROVING = (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS)


@dataclass(frozen=True)
class Structure:
    amount: Decimal
    tenure_months: int
    outcome: Outcome
    monthly_instalment: Decimal
    rate: Decimal | None
    conditions: tuple[str, ...]
    share_of_request: float

    def as_dict(self) -> dict[str, object]:
        return {
            "amount": str(self.amount), "amount_display": inr(self.amount),
            "tenure_months": self.tenure_months, "outcome": self.outcome.value,
            "monthly_instalment": str(self.monthly_instalment),
            "monthly_instalment_display": inr(self.monthly_instalment),
            "rate": str(self.rate) if self.rate else None,
            "conditions": list(self.conditions),
            "share_of_request": round(self.share_of_request, 3),
        }


@dataclass(frozen=True)
class StructuringResult:
    viable: tuple[Structure, ...]
    blocked_by: tuple[str, ...]
    note: str

    def as_dict(self) -> dict[str, object]:
        return {"viable": [s.as_dict() for s in self.viable], "blocked_by": list(self.blocked_by),
                "note": self.note}


def _blocking_clauses(case: Case) -> tuple[str, ...]:
    """Clauses that no amount or tenure can move: integrity referral and hard knockouts."""
    if not case.policy:
        return ()
    hard = {"POL-INTEGRITY", "POL-HISTORY", "POL-BOUNCES"}
    return tuple(c for c in case.policy.triggered_rules if c in hard)


def structure_options(case: Case, max_options: int = 3) -> StructuringResult:
    if not case.score or not case.policy:
        return StructuringResult((), (), "The case has not been scored yet.")

    if case.policy.outcome in APPROVING:
        return StructuringResult((), (), "The recommendation is already an approval; no restructuring needed.")

    blocking = _blocking_clauses(case)
    if blocking:
        reasons = {
            "POL-INTEGRITY": "the documents could not be verified",
            "POL-HISTORY": "the statement history is too short",
            "POL-BOUNCES": "there are too many recently returned instalments",
        }
        why = "; ".join(reasons.get(c, c) for c in blocking)
        return StructuringResult((), blocking,
                                 f"No structure can make this approvable, because {why}. "
                                 "That is a policy knockout, not a question of size.")

    # Outcomes that follow from the risk band do not move with amount or tenure: every candidate
    # structure is scored on the same band, so it would come back with the same answer. Say so,
    # and point at the improvement plan, which is the route that actually changes a band.
    band_outcome = Outcome(load_policy(case.jurisdiction)["bands"][case.score.band])
    if band_outcome not in APPROVING and "RC-LOW-REPAYMENT-CAPACITY" not in case.policy.reason_codes:
        return StructuringResult((), ("POL-BANDS",),
                                 f"Band {case.score.band} maps to {band_outcome.value.replace('_', ' ').lower()} "
                                 "whatever the amount or tenure, so no structure changes this. The improvement "
                                 "plan shows what would move the band itself.")

    sizing = load_policy(case.jurisdiction)["sizing"]
    minimum = Decimal(sizing["min_amount"])
    step = Decimal(sizing["round_down_to"])
    requested = case.requested_amount
    found: list[Structure] = []

    # Walk principal down from the request, longest tenures first: a longer tenure lowers the
    # instalment, which is usually what makes a thin-file case fit.
    for tenure in sorted(sizing["allowed_tenures"], reverse=True):
        amount = (requested / step).to_integral_value() * step
        while amount >= minimum:
            result = apply_policy(case.features, case.signals, case.score, amount, tenure, case.jurisdiction)
            if result.outcome in APPROVING and result.recommended_amount >= minimum:
                final = result.recommended_amount
                rate = result.indicative_annual_rate
                found.append(Structure(
                    amount=final, tenure_months=result.tenure_months, outcome=result.outcome,
                    monthly_instalment=emi(final, rate, result.tenure_months) if rate else Decimal("0"),
                    rate=rate, conditions=tuple(result.conditions),
                    share_of_request=float(final / requested) if requested else 0.0))
                break
            amount -= step

    # Best offer per tenure, largest principal first, then shortest tenure as a tie-break.
    unique: dict[int, Structure] = {}
    for structure in found:
        current = unique.get(structure.tenure_months)
        if current is None or structure.amount > current.amount:
            unique[structure.tenure_months] = structure
    ranked = sorted(unique.values(), key=lambda s: (-s.amount, s.tenure_months))[:max_options]

    if not ranked:
        return StructuringResult((), (), "Even at the minimum ticket the instalment does not fit inside the "
                                         "applicant's monthly surplus, so there is no viable structure today.")
    note = ("A smaller principal, or a longer tenure, brings the instalment inside policy. "
            "These are structures the unmodified policy engine approves - an underwriter still decides.")
    return StructuringResult(tuple(ranked), (), note)
