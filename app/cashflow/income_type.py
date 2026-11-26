"""Salaried or self-employed, read from the bank data instead of asked.

A salary shows up as one payer crediting a similar amount around the same date every month, usually with
SALARY / SAL / PAYROLL in the narration. Self-employment shows up as many payers, card settlements, cash
deposits and supplier payments, with no such stream. The reading is deterministic, says why it decided
what it did, and gives a confidence; below `CLEAR` a credit officer confirms it before the case is decided.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.cashflow.features import month_key
from app.domain.models import Case, Evidence, Signal, Transaction

CLEAR = 0.7  # below this the type is shown as uncertain and the policy refers the case
MIN_SALARY = Decimal("5000")
SALARY_RX = re.compile(r"\b(SALARY|SAL|SALARIES|PAYROLL|STIPEND)\b")
_STOP = {"NEFT", "IMPS", "RTGS", "UPI", "MMT", "ACH", "NACH", "ECS", "CR", "DR", "BY", "TO", "FROM", "TRANSFER", "TRF",
         "FT", "INB", "MOB", "BIL", "THE", "LTD", "PVT", "PRIVATE", "LIMITED", "SALARY", "SAL", "SALARIES", "PAYROLL",
         "STIPEND", "CREDIT", "PAYMENT", "BANK", "FOR", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
         "OCT", "NOV", "DEC", "REF", "UTR", "ATTN", "INR", "NA"}
BUSINESS_SIGNS = {"card_settlement", "cash_deposit"}
NOT_INCOME = {"self_transfer", "loan_disbursal", "nach_return"}


def payer_key(narration: str) -> str:
    """The payer's name, stable month to month: alphabetic words only, so reference numbers drop out."""
    words = [w for w in re.split(r"[^A-Z&]+", narration.upper()) if len(w) >= 3 and w not in _STOP]
    return " ".join(words[:3])


def pay_month(d: date) -> str:
    """The month a credit pays for: salaries land from the 25th onward for the next month."""
    return month_key(d + timedelta(days=8)) if d.day >= 25 else month_key(d)


def _salary_streams(credits: list[Transaction]) -> list[dict[str, Any]]:
    by_payer: dict[str, list[Transaction]] = defaultdict(list)
    for t in credits:
        if t.credit >= MIN_SALARY and t.category not in NOT_INCOME:
            by_payer[payer_key(t.narration)].append(t)
    streams = []
    for payer, rows in by_payer.items():
        months = {pay_month(t.txn_date) for t in rows}
        labelled = sum(1 for t in rows if SALARY_RX.search(t.narration.upper()))
        median = statistics.median(t.credit for t in rows)
        steady = sum(1 for t in rows if abs(t.credit - median) <= median * Decimal("0.25")) / len(rows)
        days = [t.txn_date.day + (30 if t.txn_date.day < 8 else 0) for t in rows]  # the 31st and the 2nd are one payday
        on_time = statistics.pstdev(days) <= 6 if len(days) > 1 else False
        recurring = bool(payer) and len(months) >= 3 and steady >= 0.75 and on_time and len(rows) <= len(months) + 1
        if (labelled and len(months) >= 2) or recurring:
            streams.append({"payer": payer, "rows": rows, "months": len(months), "labelled": labelled > 0})
    return sorted(streams, key=lambda s: (-s["labelled"], -sum(t.credit for t in s["rows"])))


def detect_income_type(case: Case) -> dict[str, Any]:
    """Classify, recategorise the salary credits as `salary_credit`, and return the profile with its reasons."""
    credits = [t for t in case.transactions if t.credit > 0 and t.category not in NOT_INCOME]
    months = sorted({month_key(t.txn_date) for t in case.transactions}) or ["-"]  # the statement's own months
    total_monthly = sum((t.credit for t in credits), Decimal("0")) / len(months)
    salary = None
    for stream in _salary_streams(credits):
        stream_monthly = sum((t.credit for t in stream["rows"]), Decimal("0")) / len(months)
        # A regular payer without "salary" in the narration is a salary only if it is the main income;
        # otherwise it is a regular customer of a business.
        if stream["labelled"] or (total_monthly and stream_monthly / total_monthly >= Decimal("0.5")):
            salary = stream
            break
    salary_rows = {id(t) for t in salary["rows"]} if salary else set()
    if salary:  # a salary paid early for the month after the statement ends is not one more month
        salary["months"] = len({pay_month(t.txn_date) for t in salary["rows"]} & set(months))
    for t in credits:
        if id(t) in salary_rows:
            t.category, t.category_source = "salary_credit", "RULE"

    salary_by_month: dict[str, Decimal] = defaultdict(Decimal)
    other_by_month: dict[str, Decimal] = defaultdict(Decimal)
    payers_by_month: dict[str, set[str]] = defaultdict(set)
    count_by_month: dict[str, int] = defaultdict(int)
    for t in credits:
        m = pay_month(t.txn_date)
        if id(t) in salary_rows:
            salary_by_month[m] += t.credit
        else:
            other_by_month[m] += t.credit
            payers_by_month[m].add(payer_key(t.narration))
            count_by_month[m] += 1
    salary_monthly = statistics.median(salary_by_month[m] for m in months) if salary else Decimal("0")
    other_monthly = statistics.median(other_by_month[m] for m in months)
    share = float(salary_monthly / (salary_monthly + other_monthly)) if salary_monthly + other_monthly else 0.0

    # Marks of a business, each independent; UPI payments to shops are left out, everyone makes those.
    payers = statistics.median(len(payers_by_month[m]) for m in months)
    receipts = statistics.median(count_by_month[m] for m in months)
    other_total = sum(other_by_month.values()) or Decimal("1")
    cash_card = sum((t.credit for t in credits if t.category in BUSINESS_SIGNS), Decimal("0")) / other_total
    employer_months = len({pay_month(t.txn_date) for t in case.transactions
                           if t.debit > 0 and t.category in ("staff_salary", "tax_payment")})
    signs = [(payers >= 5, f"money comes in from about {payers:.0f} different payers a month"),
             (receipts >= 15, f"about {receipts:.0f} receipts a month"),
             (cash_card >= Decimal("0.1"), f"{cash_card:.0%} of receipts are cash deposits or card settlements"),
             (employer_months >= 2, "pays staff wages or GST")]
    business = [text for hit, text in signs if hit]

    reasons: list[str] = []
    if salary:
        reasons.append(f"{salary['payer'].title() or 'One payer'} credits about ₹{salary_monthly:,.0f} in "
                       f"{salary['months']} of {len(months)} months, around the same date"
                       + (", marked as salary" if salary["labelled"] else ""))
    if business:
        reasons.append("Business activity: " + "; ".join(business))

    if salary and share >= 0.6 and salary["months"] >= min(3, len(months)):
        kind = "salaried"
        confidence = 0.95 if salary["labelled"] and salary["months"] >= len(months) - 1 else 0.8
        if len(business) >= 2:
            confidence = 0.6
            reasons.append("There is also business activity, so a side business is possible")
    elif salary:  # a salary, but most money comes from elsewhere: a side business, or salary plus rent
        kind, confidence = ("salaried" if share >= 0.5 else "self_employed"), 0.55
        reasons.append(f"Salary is only {share:.0%} of income, so the main source is not clear-cut")
    else:
        kind = "self_employed"
        confidence = {0: 0.5, 1: 0.75}.get(len(business), 0.9)
        if not business:
            reasons.append("No salary pattern, and too little business activity to be sure")

    previous = case.income_profile
    return {
        "type": kind, "confidence": confidence, "reasons": reasons,
        "employer": salary["payer"].title() if salary else None,
        "salary_monthly": float(salary_monthly), "salary_months": salary["months"] if salary else 0,
        "other_income_monthly": float(other_monthly), "salary_share": round(share, 4),
        # What the customer told us after the reading, and an officer's correction, survive a re-run.
        **{k: previous[k] for k in ("declared", "override") if k in previous},
    }


def assessed_monthly_income(case: Case) -> float:
    """What the instalment is sized on: net salary if salaried (all income if no salary stream was found, e.g.
    after an officer override), the business's monthly surplus if self-employed."""
    f = case.features
    if case.income_type == "salaried":
        return case.income_profile.get("salary_monthly") or f.get("median_monthly_inflow", 0.0)
    return max(0.0, f.get("monthly_surplus", 0.0))


def preview_hint(parsed: Any) -> dict[str, Any]:
    """The reading on one uploaded statement, before submission, so the customer is asked only what fits."""
    from app.cashflow.categorise import apply_rules
    from app.documents.ingest import attach_parsed_statement

    probe = Case(case_id="preview", owner_uid="-", business_name="-", requested_amount=Decimal(1), tenure_months=12,
                 product="used_car")
    attach_parsed_statement(probe, "preview", "preview", b"", parsed, "template")
    apply_rules(probe.transactions)
    profile = detect_income_type(probe)
    return {k: profile[k] for k in ("type", "confidence", "employer", "salary_monthly")}


def income_evidence_and_signals(case: Case) -> list[Signal]:
    profile = case.income_profile
    salary_ids = [t.evidence_id for t in case.transactions if t.category == "salary_credit"]
    if salary_ids:
        first = next(t for t in case.transactions if t.evidence_id == salary_ids[0])
        case.evidence["ev-computed-salary"] = Evidence(
            evidence_id="ev-computed-salary", doc_id=first.doc_id, page=first.page, kind="COMPUTED",
            description=f"Salary credits from {profile.get('employer') or 'the employer'}", derived_from=salary_ids)
    evidence = ["ev-computed-salary"] if salary_ids else (["ev-computed-inflow"] if "ev-computed-inflow" in case.evidence else [])
    signals = []
    if profile.get("confidence", 1) < CLEAR and not profile.get("override"):
        signals.append(Signal(signal_id="sig-rk-income-type", code="RK-INCOME-TYPE-UNCLEAR", category="RISK",
                              severity="MEDIUM", confidence=1 - profile["confidence"], evidence_ids=evidence,
                              title="Salaried or self-employed is not clear",
                              explanation="; ".join(profile.get("reasons", [])) + ". A credit officer should confirm it."))
    if case.income_type == "salaried" and profile.get("salary_months", 0) >= 6:
        signals.append(Signal(signal_id="sig-pos-salary", code="POS-REGULAR-SALARY", category="POSITIVE",
                              severity="INFO", confidence=0.95, evidence_ids=evidence,
                              title="Regular monthly salary",
                              explanation=f"Salary from {profile.get('employer') or 'one employer'} every month, about "
                                          f"₹{profile['salary_monthly']:,.0f}."))
    return signals
