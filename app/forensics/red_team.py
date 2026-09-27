"""Adversarial red-team agent.

Our forensic rules were written by the same people who wrote the tampering they are measured
against, which is why the evaluation report refuses to call its own scores evidence of accuracy.
This agent attacks that problem directly: it forges statements on purpose, in a loop, adapting
after every miss, and reports the point at which our detectors stop working.

The output is the number that actually matters to a risk committee: **the smallest edit we fail to
catch.** Running it is how we find out whether a control is real.

It is a planner-executor-critic loop over a deterministic environment:
  plan    - choose the next attack (pattern, magnitude, how many rows)
  execute - generate that statement and run the real pipeline's detectors over it
  critique- did a corroborated severe finding fire? if not, the attack survived
  adapt   - on success for us, press harder; on failure, back off toward the boundary

No model is required, which is deliberate: the loop must run in CI on every commit. When AI mode
is on, Gemini proposes attack narratives that the deterministic generator then executes, so the
search can explore patterns we did not think of without ever trusting a model to score itself.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from app.cashflow.categorise import apply_rules
from app.cashflow.features import add_computed_evidence, compute_features
from app.documents.ingest import attach_parsed_statement
from app.documents.pdf_reader import read_statement
from app.domain.models import Case
from app.forensics.rules import run_rules
from app.security.injection import detect
from app.synthetic.generator import (Profile, Statement, add_injection, mark_editor_resave, render_pdf,
                                     statement_for, tamper_inflate_credits)

SEVERE = ("HIGH", "CRITICAL")


@dataclass(frozen=True)
class Attack:
    pattern: str
    magnitude: int          # rupees added per edited row
    rows: int               # how many rows are edited
    repair_balances: bool   # does the forger fix every later balance
    resave: bool            # does the file go through an editor afterwards


@dataclass
class AttackResult:
    attack: Attack
    detected: bool          # a corroborated HIGH/CRITICAL finding: the case is referred to integrity review
    flagged: bool           # any corroborated fraud finding, including a MEDIUM verification trigger
    codes: tuple[str, ...]
    inflation_share: float  # how much the edit lifted reported inflow


@dataclass
class RedTeamReport:
    trials: list[AttackResult] = field(default_factory=list)

    @property
    def evasions(self) -> list[AttackResult]:
        """Attacks that produced no finding at all - neither a referral nor a verification trigger."""
        return [t for t in self.trials if not t.flagged]

    @property
    def detection_rate(self) -> float:
        """Share of attacks that triggered an integrity referral."""
        return sum(1 for t in self.trials if t.detected) / max(1, len(self.trials))

    @property
    def flag_rate(self) -> float:
        """Share of attacks that raised any finding an underwriter would see."""
        return sum(1 for t in self.trials if t.flagged) / max(1, len(self.trials))

    def smallest_evasion(self) -> AttackResult | None:
        """The cheapest successful attack — the honest headline number."""
        survivors = self.evasions
        return min(survivors, key=lambda t: t.attack.magnitude * t.attack.rows) if survivors else None

    def as_dict(self) -> dict[str, object]:
        smallest = self.smallest_evasion()
        return {
            "trials": len(self.trials),
            "referral_rate": round(self.detection_rate, 4),
            "flag_rate": round(self.flag_rate, 4),
            "evasions": len(self.evasions),
            "smallest_evasion": None if not smallest else {
                "pattern": smallest.attack.pattern, "magnitude": smallest.attack.magnitude,
                "rows": smallest.attack.rows, "repair_balances": smallest.attack.repair_balances,
                "resave": smallest.attack.resave,
                "inflation_share": round(smallest.inflation_share, 4),
            },
            "referral_by_pattern": {
                pattern: round(
                    sum(1 for t in self.trials if t.attack.pattern == pattern and t.detected)
                    / max(1, sum(1 for t in self.trials if t.attack.pattern == pattern)), 4)
                for pattern in sorted({t.attack.pattern for t in self.trials})
            },
            "flag_by_pattern": {
                pattern: round(
                    sum(1 for t in self.trials if t.attack.pattern == pattern and t.flagged)
                    / max(1, sum(1 for t in self.trials if t.attack.pattern == pattern)), 4)
                for pattern in sorted({t.attack.pattern for t in self.trials})
            },
        }


def _base_profile(seed: int) -> Profile:
    rng = random.Random(seed)
    return Profile(
        name=f"red{seed}", holder=f"RED TEAM TARGET {seed:03d}", bank="Northstar Bank", seed=seed,
        opening_balance=rng.uniform(30000, 120000), neft_credits_per_month=rng.randint(6, 12),
        neft_credit_mean=rng.uniform(25000, 60000), cash_deposits_per_month=rng.randint(0, 3),
        cash_deposit_mean=rng.uniform(5000, 18000), supplier_payments_per_month=rng.randint(6, 10),
        supplier_payment_share=rng.uniform(0.5, 0.75), rent=rng.choice([12000, 18000, 25000]),
        utilities=rng.uniform(2500, 6000))


def _forge(attack: Attack, seed: int) -> Statement:
    """Build one forged statement. Magnitudes are applied exactly, not sampled, so the search is
    a real boundary search rather than noise."""
    statement = statement_for(_base_profile(seed))
    rng = random.Random(seed * 31 + attack.magnitude)
    if attack.pattern == "injection":
        add_injection(statement, rng)
        return statement
    _inflate_exact(statement, rng, attack)
    if attack.resave:
        mark_editor_resave(statement)
    return statement


def _inflate_exact(statement: Statement, rng: random.Random, attack: Attack) -> None:
    credits = [i for i, row in enumerate(statement.rows) if row.credit > 0]
    picks = sorted(rng.sample(credits, k=min(attack.rows, len(credits))))
    bump = Decimal(attack.magnitude)
    # A slightly different font on the edited cell is what a real editor leaves behind.
    fonts = [("Helvetica", 9.2), ("Times-Roman", 8.5), ("Helvetica", 8.8)]
    for n, index in enumerate(picks):
        row = statement.rows[index]
        row.credit += bump
        row.amount_font = fonts[n % len(fonts)] if attack.pattern != "retypeset" else None
        if attack.repair_balances:
            for later in statement.rows[index:]:
                later.balance += bump
        else:
            row.balance += bump
    statement.labels.tampered = True
    statement.labels.tampered_row_indices.extend(picks)
    statement.labels.tamper_types.append(attack.pattern)


def run_attack(attack: Attack, seed: int) -> AttackResult:
    """Forge, then run the production detectors over the result."""
    statement = _forge(attack, seed)
    data = render_pdf(statement)
    parsed = read_statement(data)
    case = Case(case_id=f"red-{seed}", owner_uid="redteam", business_name="Red team",
                requested_amount=Decimal("500000"), tenure_months=24)
    attach_parsed_statement(case, "d1", "forged.pdf", data, parsed, "template")
    for txn in case.transactions:
        txn.redacted = bool(detect(txn.narration))
    apply_rules(case.transactions)
    case.features, case.monthly = compute_features(case)
    add_computed_evidence(case)
    case.signals = run_rules(case)
    fraud = [s for s in case.signals if s.category == "FRAUD" and s.corroborated]
    severe = [s for s in fraud if s.severity in SEVERE]
    lift = (attack.magnitude * attack.rows) / max(1.0, case.features["median_monthly_inflow"] * 6)
    return AttackResult(attack=attack, detected=bool(severe), flagged=bool(fraud),
                        codes=tuple(sorted({s.code for s in fraud})), inflation_share=lift)


def hunt(seeds: int = 6, patterns: tuple[str, ...] = ("naive", "repaired", "retypeset", "injection")) -> RedTeamReport:
    """Binary-search each pattern for the largest edit that still escapes detection."""
    report = RedTeamReport()
    for pattern in patterns:
        for seed in range(1, seeds + 1):
            if pattern == "injection":
                report.trials.append(run_attack(Attack(pattern, 0, 0, False, False), seed))
                continue
            low, high = 2_000, 400_000       # rupees added per row
            repaired = pattern != "naive"
            for _ in range(6):               # binary search on magnitude
                mid = (low + high) // 2
                attack = Attack(pattern=pattern, magnitude=mid, rows=2, repair_balances=repaired,
                                resave=False)
                result = run_attack(attack, seed)
                report.trials.append(result)
                if result.detected:
                    high = mid               # detected: try a subtler edit
                else:
                    low = mid                # survived: press harder
    return report
