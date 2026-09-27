"""Labels for a world where nobody has repaid anything yet.

A credit model needs outcomes, and synthetic borrowers never default. So we simulate repayment
from the *latent* parameters of each generated business — its true volatility, true buffer, true
obligation burden — and never from the features the model will see or from our own scorecard.
That matters: if the label were a function of the scorecard, any model trained on it would just
be relearning the scorecard, and the measured uplift would be an artefact.

The latent process deliberately contains one interaction a linear scorecard cannot represent:
volatility only really hurts when the balance buffer is thin. A business with lumpy income and
three months of cash survives; the same business living hand-to-mouth does not. That is a real
credit phenomenon, and it is what a gradient-boosted model should find and a linear model should
miss. The measured Gini uplift is exactly the value of that non-linearity, nothing more.

None of this is evidence about real borrowers. It is a controlled environment for proving the
training, calibration, monitoring and fallback machinery works end to end.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal

from app.domain.models import Case, Evidence, Transaction
from app.synthetic.generator import Profile, Statement

LATENT = {
    "intercept": -4.75,
    "obligation": 2.6,      # instalments already committed against income
    "volatility": 1.5,      # month-to-month swing in income
    "thin_buffer": 1.7,     # low opening balance relative to monthly spend
    "returns": 0.62,        # instalments already bouncing
    "trend": -1.1,          # growing businesses repay better
    "cash_reliance": 0.5,   # cash-heavy income is harder to sustain
    "interaction": 2.3,     # volatility x thin buffer: the non-linear term
}


@dataclass(frozen=True)
class SimulatedOutcome:
    defaulted: bool
    latent_pd: float


def _latent_inputs(profile: Profile, rng: random.Random) -> dict[str, float]:
    monthly_income = (profile.daily_upi_mean * 26 if profile.daily_upi_mean
                      else profile.neft_credits_per_month * profile.neft_credit_mean)
    monthly_income += profile.cash_deposits_per_month * profile.cash_deposit_mean
    monthly_income = max(monthly_income, 1.0)
    monthly_spend = monthly_income * profile.supplier_payment_share + profile.rent + profile.utilities \
        + sum(profile.staff_payments)
    seasonal = [abs(1.0 - v) for v in profile.seasonality.values()] or [0.0]
    cash_share = (profile.cash_deposits_per_month * profile.cash_deposit_mean) / monthly_income
    return {
        "obligation": min(1.0, profile.emi_amount / monthly_income),
        "volatility": min(1.2, max(seasonal) + (0.25 if profile.daily_upi_mean == 0 else 0.05)),
        "thin_buffer": min(1.0, max(0.0, 1.0 - profile.opening_balance / max(1.0, monthly_spend))),
        "returns": float(len(profile.nach_return_months)),
        "trend": rng.uniform(-0.3, 0.3),
        "cash_reliance": min(1.0, cash_share),
    }


def simulate_outcome(profile: Profile, rng: random.Random | None = None) -> SimulatedOutcome:
    rng = rng or random.Random(profile.seed * 977 + 7)
    x = _latent_inputs(profile, rng)
    logit = LATENT["intercept"]
    for name in ("obligation", "volatility", "thin_buffer", "returns", "trend", "cash_reliance"):
        logit += LATENT[name] * x[name]
    logit += LATENT["interaction"] * x["volatility"] * x["thin_buffer"]
    logit += rng.gauss(0, 0.45)  # idiosyncratic shocks: illness, a lost customer, a bad season
    pd = 1 / (1 + math.exp(-logit))
    return SimulatedOutcome(defaulted=rng.random() < pd, latent_pd=pd)


def build_training_case(statement: Statement, index: int) -> Case:
    """Build a Case straight from generated rows, skipping PDF render and parse.

    Training needs thousands of cases and does not need pixels. Forensics is not run here: the
    integrity layer is evaluated separately in `evals/run_evals.py`, and mixing a tampered
    statement into credit-model training would teach the model to predict our own detectors.
    """
    case = Case(case_id=f"train-{index}", owner_uid="training", business_name=f"Train {index}",
                requested_amount=Decimal("500000"), tenure_months=24)
    from app.domain.models import Document, StatementHeader

    case.documents.append(Document(
        doc_id="d1", filename="generated.pdf", sha256="", size_bytes=0, page_count=1, parser="template",
        header=StatementHeader(bank_name=statement.bank_name, account_holder=statement.holder,
                               account_last4=statement.account_last4, period_from=statement.period_from,
                               period_to=statement.period_to, opening_balance=statement.opening_balance)))
    for row_index, row in enumerate(statement.rows):
        evidence_id = f"ev-d1-p1-r{row_index}"
        case.evidence[evidence_id] = Evidence(evidence_id=evidence_id, doc_id="d1", page=1, bbox=[0, 0, 1, 1],
                                              kind="TXN_ROW", description="generated row")
        case.transactions.append(Transaction(
            txn_id=f"t-{row_index}", doc_id="d1", page=1, row_index=row_index, txn_date=row.txn_date,
            narration=row.narration, reference=row.ref, debit=row.debit, credit=row.credit,
            balance=row.balance, evidence_id=evidence_id))
    return case
