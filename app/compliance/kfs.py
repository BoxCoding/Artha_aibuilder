"""Key Fact Statement (KFS) for an approved loan.

RBI's digital lending framework requires a standardised Key Fact Statement before the loan contract is
executed: amount, tenure, instalments, all fees, the Annual Percentage Rate (APR) covering every cost,
the look-up (cooling-off) period, and grievance redressal details. LedgerLens generates it the moment a
credit officer approves, the customer must accept it before disbursal, and its digest is recorded.

APR here is the annualised internal rate of return of the cash flows the borrower actually sees: the
net amount received (principal less fees and GST) against the instalments paid.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.config import get_settings
from app.domain.formatting import emi as emi_amount


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def monthly_irr(net: float, instalment: float, months: int) -> float:
    """Solve net = sum(instalment / (1 + i)^k) for i by bisection (monotone, always converges)."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        pv = instalment * (1 - (1 + mid) ** -months) / mid if mid else instalment * months
        if pv > net:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def build_kfs(case_id: str, principal: Decimal, annual_rate: Decimal, months: int,
              business_name: str) -> dict[str, Any]:
    settings = get_settings()
    instalment = Decimal(emi_amount(principal, annual_rate, months))
    fee = (principal * Decimal(str(settings.processing_fee_pct)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    gst = (fee * Decimal(str(settings.gst_on_fees_pct)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    net = principal - fee - gst
    total_repaid = instalment * months
    irr = monthly_irr(float(net), float(instalment), months)
    now = datetime.now(timezone.utc)
    schedule = []
    balance = principal
    monthly_rate = annual_rate / 12
    for k in range(1, months + 1):
        interest = (balance * monthly_rate).quantize(Decimal("0.01"))
        principal_part = instalment - interest
        balance = max(Decimal("0"), balance - principal_part)
        if k <= 3 or k == months:
            schedule.append({"instalment": k, "emi": _money(instalment), "interest": _money(interest),
                             "principal": _money(principal_part), "balance_after": _money(balance)})
    body = {
        "kfs_version": "rbi-dl-kfs.v1",
        "lender": settings.lender_name,
        "borrower_business": business_name,
        "case_id": case_id,
        "generated_at": now.isoformat(timespec="seconds"),
        "valid_until": (now + timedelta(days=3)).isoformat(timespec="seconds"),
        "loan_type": "Unsecured MSME working-capital term loan",
        "sanctioned_amount": _money(principal),
        "tenure_months": months,
        "instalment_frequency": "Monthly",
        "number_of_instalments": months,
        "emi": _money(instalment),
        "interest_rate_annual_pct": str((annual_rate * 100).quantize(Decimal("0.01"))),
        "interest_type": "Fixed, reducing balance",
        "processing_fee": _money(fee),
        "gst_on_fees": _money(gst),
        "net_disbursed_amount": _money(net),
        "total_interest": _money(total_repaid - principal),
        "total_amount_repayable": _money(total_repaid),
        "apr_pct": f"{irr * 12 * 100:.2f}",
        "cooling_off_days": settings.cooling_off_days,
        "cooling_off_terms": (f"You may exit within {settings.cooling_off_days} days of disbursal by repaying the "
                              "principal and the proportionate APR, with no penalty."),
        "prepayment": "Allowed at any time without a foreclosure charge.",
        "penal_charges": "Penal charges, if any, apply only on the overdue instalment and are not compounded.",
        "recovery": "Recovery agents are not engaged without prior written notice to you.",
        "grievance_redressal": settings.grievance_officer,
        "schedule_excerpt": schedule,
    }
    body["kfs_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body
