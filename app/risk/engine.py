"""Deterministic risk engine: scorecard -> policy -> sizing -> improvement plan.

No language model participates in any function in this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.domain.models import ImprovementStep, Outcome, PolicyResult, ReasonCode, ScoreResult, Signal

CONFIG_DIR = Path(__file__).parent / "config"
SEVERITY_ORDER = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
OUTCOME_RANK = {Outcome.APPROVE: 0, Outcome.APPROVE_WITH_CONDITIONS: 1, Outcome.REFER: 2,
                Outcome.DECLINE: 3, Outcome.REFER_INTEGRITY_REVIEW: 4}


@lru_cache
def load_scorecard() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "scorecard_v1.yaml").read_text(encoding="utf-8"))


@lru_cache
def load_policy(jurisdiction: str = "IN") -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / f"policy_{jurisdiction.lower()}.yaml").read_text(encoding="utf-8"))


def _transform(value: float, spec: dict[str, Any]) -> float:
    return min(max(value, spec["cap_low"]), spec["cap_high"])


def score(features: dict[str, float]) -> ScoreResult:
    card = load_scorecard()
    logit = card["intercept"]
    contributions: list[ReasonCode] = []
    for name, spec in card["features"].items():
        x = _transform(features[name], spec)
        logit += spec["weight"] * x
        delta = spec["weight"] * (x - spec["reference"])
        if abs(delta) >= 0.02:
            contributions.append(ReasonCode(
                code=spec["code"], feature=name, contribution=round(delta, 4), value=round(features[name], 4),
                direction="increases_risk" if delta > 0 else "decreases_risk"))
    pd = 1 / (1 + math.exp(-logit))
    band = next(b for b, upper in card["bands"].items() if pd < upper)
    reasons = sorted((c for c in contributions if c.direction == "increases_risk"), key=lambda c: -c.contribution)
    strengths = sorted((c for c in contributions if c.direction == "decreases_risk"), key=lambda c: c.contribution)
    return ScoreResult(scorecard_version=card["version"], pd=round(pd, 4), band=band,  # type: ignore[arg-type]
                       reasons=reasons[:4], strengths=strengths[:3])


@dataclass(frozen=True)
class SizingResult:
    amount: Decimal
    emi_capacity: Decimal
    rate: Decimal


def annuity_principal(emi: Decimal, annual_rate: Decimal, months: int) -> Decimal:
    r = annual_rate / Decimal(12)
    if r == 0:
        return emi * months
    factor = (1 - (1 + r) ** (-months)) / r
    return emi * factor


def size_loan(features: dict[str, float], requested: Decimal, tenure: int, band: str,
              policy: dict[str, Any]) -> SizingResult:
    s = policy["sizing"]
    rate = Decimal(s["indicative_annual_rate"][band])
    surplus = Decimal(str(features["monthly_surplus"]))
    existing = Decimal(str(features["existing_emi_monthly"]))
    capacity = max(Decimal("0"), Decimal(s["surplus_share"]) * surplus - existing)
    candidates = [requested, Decimal(s["max_amount"]),
                  Decimal(s["abb_multiple"]) * Decimal(str(features["avg_daily_balance"])),
                  annuity_principal(capacity, rate, tenure)]
    step = Decimal(s["round_down_to"])
    amount = (min(candidates) / step).to_integral_value(rounding=ROUND_DOWN) * step
    return SizingResult(amount=max(Decimal("0"), amount), emi_capacity=capacity.quantize(Decimal("1")), rate=rate)


def _compare(value: float, op: str, threshold: float) -> bool:
    return {"lt": value < threshold, "lte": value <= threshold, "gt": value > threshold,
            "gte": value >= threshold, "eq": value == threshold}[op]


def _signal_match(signals: list[Signal], category: str, min_severity: str) -> list[Signal]:
    return [s for s in signals if s.category == category and s.corroborated
            and SEVERITY_ORDER[s.severity] >= SEVERITY_ORDER[min_severity]]


def apply_policy(features: dict[str, float], signals: list[Signal], result: ScoreResult, requested: Decimal,
                 tenure: int, jurisdiction: str = "IN") -> PolicyResult:
    policy = load_policy(jurisdiction)
    tenure = tenure if tenure in policy["sizing"]["allowed_tenures"] else max(policy["sizing"]["allowed_tenures"])
    outcome = Outcome(policy["bands"][result.band])
    triggered = ["POL-BANDS"]
    reason_codes = [r.code for r in result.reasons[:3]]
    conditions = list(policy.get("band_conditions", {}).get(result.band, []))

    def escalate(new: Outcome, rule: dict[str, Any]) -> None:
        nonlocal outcome
        triggered.append(rule["clause"])
        if rule.get("reason_code") and rule["reason_code"] not in reason_codes:
            reason_codes.insert(0, rule["reason_code"])
        if rule.get("condition"):
            conditions.append(rule["condition"])
        if OUTCOME_RANK[new] > OUTCOME_RANK[outcome]:
            outcome = new

    for rule in policy["knockouts"]:
        if "signal_category" in rule and _signal_match(signals, rule["signal_category"], rule["min_severity"]):
            escalate(Outcome(rule["outcome"]), rule)
        elif "feature" in rule and _compare(features[rule["feature"]], rule["op"], rule["value"]):
            escalate(Outcome(rule["outcome"]), rule)
    if outcome != Outcome.REFER_INTEGRITY_REVIEW:
        for rule in policy["soft_rules"]:
            if "signal_code" in rule:
                if any(s.code == rule["signal_code"] for s in signals):
                    escalate(Outcome(rule["outcome"]), rule)
            elif _signal_match(signals, rule["signal_category"], rule["min_severity"]):
                escalate(Outcome(rule["outcome"]), rule)

    sizing = size_loan(features, requested, tenure, result.band, policy)
    triggered.append("POL-SIZING")
    if outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS) and sizing.amount < Decimal(policy["sizing"]["min_amount"]):
        outcome = Outcome.DECLINE
        reason_codes.insert(0, "RC-LOW-REPAYMENT-CAPACITY")
    if sizing.amount > Decimal(policy["four_eyes_above"]) and outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS):
        triggered.append("POL-FOUR-EYES")
    eligible = outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS)
    return PolicyResult(
        policy_id=policy["policy_id"], policy_version=str(policy["version"]), outcome=outcome,
        triggered_rules=list(dict.fromkeys(triggered)), reason_codes=list(dict.fromkeys(reason_codes))[:4],
        recommended_amount=sizing.amount if eligible else Decimal("0"), max_emi_capacity=sizing.emi_capacity,
        indicative_annual_rate=sizing.rate if eligible else None, tenure_months=tenure,
        conditions=list(dict.fromkeys(conditions)))


def improvement_plan(features: dict[str, float], current: ScoreResult, max_steps: int = 8) -> list[ImprovementStep]:
    """Greedy search over actionable features for the smallest change set that improves the band."""
    card = load_scorecard()
    target_rank = "ABCDE".index(current.band) - 1
    if target_rank < 0:
        return []
    trial = dict(features)
    changed: dict[str, float] = {}
    for _ in range(max_steps):
        best: tuple[float, str, float] | None = None
        for name, spec in card["features"].items():
            if not spec.get("actionable"):
                continue
            new = trial[name] + spec["step"]
            if "target_floor" in spec:
                new = max(new, spec["target_floor"])
            if "target_ceiling" in spec:
                new = min(new, spec["target_ceiling"])
            if new == trial[name]:
                continue
            candidate = dict(trial, **{name: new})
            gain = current.pd - score(candidate).pd
            if gain > 0 and (best is None or gain > best[0]):
                best = (gain, name, new)
        if best is None:
            break
        _, name, new = best
        trial[name] = new
        changed[name] = new
        if "ABCDE".index(score(trial).band) <= target_rank:
            break
    return [ImprovementStep(feature=n, current=round(features[n], 4), target=round(v, 4),
                            hint_key=card["features"][n]["code"]) for n, v in changed.items()]


def challenge_score(features: dict[str, float], result: ScoreResult) -> tuple[dict[str, float | str | bool], Signal | None]:
    """Run the learned challenger beside the scorecard and raise a signal when they disagree.

    The scorecard still decides. A material disagreement is treated as information - it means a
    linear view and a learned view part company on this case - and sends it to a human.
    """
    from app.risk.ml_model import DISAGREEMENT_BANDS, load_model

    from app.risk.ml_model import predict_remote

    model = load_model()
    if model is None:
        return {"available": False}, None
    served_by = "local"
    try:
        remote = predict_remote(features)  # Vertex AI endpoint when configured
        if remote is not None:
            ml_pd, served_by = remote, "vertex_ai_endpoint"
        else:
            ml_pd = model.predict_pd(features)
    except Exception:  # noqa: BLE001 - never let the challenger break the decision path
        return {"available": False, "error": "prediction_failed"}, None

    card = load_scorecard()
    ml_band = next(b for b, upper in card["bands"].items() if ml_pd < upper)
    distance = abs("ABCDE".index(ml_band) - "ABCDE".index(result.band))
    delta = ml_pd - result.pd
    disagrees = distance >= DISAGREEMENT_BANDS
    summary: dict[str, float | str | bool] = {
        "available": True, "version": model.version, "ml_pd": round(ml_pd, 4),
        "scorecard_pd": result.pd, "delta": round(delta, 4), "disagrees": disagrees,
        "ml_band": ml_band, "scorecard_band": result.band, "band_distance": distance,
        "gini_uplift": model.metrics.gini_uplift, "served_by": served_by,
    }
    if not disagrees:
        return summary, None
    direction = "higher" if delta > 0 else "lower"
    return summary, Signal(
        signal_id="sig-rk-model-disagree", code="RK-MODEL-DISAGREE", category="RISK", severity="MEDIUM",
        confidence=0.7, detector="RULE", corroborated=True, evidence_ids=["ev-computed-inflow"],
        title="The two risk models disagree",
        explanation=(f"The learned challenger model puts this application in band {ml_band} ({ml_pd:.1%} default "
                     f"risk), {direction} than the scorecard's band {result.band} ({result.pd:.1%}). Cases where "
                     "the two models part company by two bands or more are assessed by a person."),
    )


def band_rate(band: str, jurisdiction: str = "IN") -> Decimal:
    """Indicative annual rate for a band, used when an underwriter approves a case the engine did
    not recommend for approval (the engine leaves the rate unset in that situation)."""
    return Decimal(load_policy(jurisdiction)["sizing"]["indicative_annual_rate"][band])


def clause_text(clause_id: str, jurisdiction: str = "IN") -> str:
    return load_policy(jurisdiction)["clauses"][clause_id]
