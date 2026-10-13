"""Cash-flow features. All arithmetic is deterministic; floats are used only for ratios."""
from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.cashflow.categorise import BUSINESS_CREDIT, DIGITAL_CREDIT
from app.domain.models import Case, Evidence, Signal, Transaction

FEATURE_SCHEMA_VERSION = "features.v2"


def _month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def span_months(start: date, end: date) -> float:
    """Calendar months covered from start to end inclusive, with the final part-month as a fraction:
    1 Mar-31 Aug is 6.0, 2 Mar-29 Aug is 5.9, 31 Mar-1 Aug is 4.1. Counting the distinct months a
    statement touches instead would score the last two as six."""
    stop = end + timedelta(days=1)
    whole = (stop.year - start.year) * 12 + stop.month - start.month
    if _add_months(start, whole) > stop:
        whole -= 1
    anchor, following = _add_months(start, whole), _add_months(start, whole + 1)
    return round(whole + (stop - anchor).days / (following - anchor).days, 2)


def compute_features(case: Case) -> tuple[dict[str, float], list[dict[str, str | float]]]:
    txns = sorted(case.transactions, key=lambda t: (t.txn_date, t.doc_id, t.row_index))
    if not txns:
        raise ValueError("no transactions to analyse")
    header = case.documents[0].header
    start = header.period_from or txns[0].txn_date
    end = header.period_to or txns[-1].txn_date
    months = sorted({_month_key(start + timedelta(days=i)) for i in range((end - start).days + 1)})

    inflow: dict[str, Decimal] = defaultdict(Decimal)
    opex: dict[str, Decimal] = defaultdict(Decimal)
    emi: dict[str, Decimal] = defaultdict(Decimal)
    returns: dict[str, int] = defaultdict(int)
    cash = digital = Decimal("0")
    counterparties: dict[str, Decimal] = defaultdict(Decimal)
    for t in txns:
        m = _month_key(t.txn_date)
        if t.credit > 0 and t.category in BUSINESS_CREDIT:
            inflow[m] += t.credit
            cash += t.credit if t.category == "cash_deposit" else 0
            digital += t.credit if t.category in DIGITAL_CREDIT else 0
            if t.category == "neft_imps_credit" and "/" in t.narration:
                counterparties[t.narration.split("/", 1)[1].strip()] += t.credit
        elif t.category == "nach_return":
            returns[m] += 1
            emi[m] -= t.credit  # returned EMI was never actually paid
        elif t.category == "emi_debit":
            emi[m] += t.debit
        elif t.debit > 0 and t.category not in {"self_transfer"}:
            opex[m] += t.debit

    daily = _daily_balances(txns, header.opening_balance, start, end)
    median_inflow = float(statistics.median([inflow[m] for m in months])) if months else 0.0
    inflows = [float(inflow[m]) for m in months]
    total_business = float(sum(inflow.values())) or 1.0
    avg_balance = statistics.fmean(daily.values()) if daily else 0.0
    low_threshold = max(2000.0, 0.05 * median_inflow)
    third = max(1, len(months) // 2)
    first, last = statistics.fmean(inflows[:third]), statistics.fmean(inflows[-third:])
    emi_monthly = float(sum(emi.values())) / max(1, len(months))
    surplus = statistics.median([float(inflow[m] - opex[m]) for m in months]) if months else 0.0
    recent_months = months[-3:]

    features = {
        "statement_span_months": span_months(start, end),
        "median_monthly_inflow": round(median_inflow, 2),
        "avg_daily_balance": round(avg_balance, 2),
        "abb_to_inflow": round(avg_balance / median_inflow, 4) if median_inflow else 0.0,
        "inflow_cv": round(statistics.pstdev(inflows) / statistics.fmean(inflows), 4) if any(inflows) else 1.0,
        "inflow_trend": round(last / first - 1, 4) if first else 0.0,
        "cash_deposit_share": round(float(cash) / total_business, 4),
        "digital_share": round(float(digital) / total_business, 4),
        "nach_returns_3m": float(sum(returns[m] for m in recent_months)),
        "nach_returns_6m": float(sum(returns.values())),
        "existing_emi_monthly": round(max(0.0, emi_monthly), 2),
        "obligation_ratio": round(max(0.0, emi_monthly) / median_inflow, 4) if median_inflow else 0.0,
        "monthly_surplus": round(surplus, 2),
        "low_balance_days_share": round(sum(1 for v in daily.values() if v < low_threshold) / max(1, len(daily)), 4),
        "top_counterparty_share": round(float(max(counterparties.values())) / total_business, 4) if counterparties else 0.0,
    }
    monthly = [{
        "month": m, "inflow": float(inflow[m]), "outflow": float(opex[m] + max(emi[m], Decimal("0"))),
        "avg_balance": round(statistics.fmean([v for d, v in daily.items() if _month_key(d) == m] or [0.0]), 2),
        "nach_returns": float(returns[m]),
    } for m in months]
    return features, monthly


def _daily_balances(txns: list[Transaction], opening: Decimal | None, start: date, end: date) -> dict[date, float]:
    eod: dict[date, Decimal] = {}
    for t in txns:
        eod[t.txn_date] = t.balance
    balance = opening if opening is not None else (txns[0].balance - txns[0].credit + txns[0].debit)
    out: dict[date, float] = {}
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        balance = eod.get(d, balance)
        out[d] = float(balance)
    return out


def add_computed_evidence(case: Case) -> None:
    """Evidence entries for aggregate figures, pointing at the rows they were computed from."""
    def rows(pred) -> list[str]:  # type: ignore[no-untyped-def]
        return [t.evidence_id for t in case.transactions if pred(t)]

    doc_id = case.documents[0].doc_id
    specs = {
        "ev-computed-inflow": ("Monthly business inflows (UPI, bank transfers, card settlements, cash deposits)",
                               rows(lambda t: t.category in BUSINESS_CREDIT)),
        "ev-computed-balance": ("Daily closing balances across the statement period", rows(lambda t: True)),
        "ev-computed-emi": ("Loan instalments debited (EMI / NACH)", rows(lambda t: t.category == "emi_debit")),
        "ev-computed-returns": ("Instalments returned unpaid", rows(lambda t: t.category == "nach_return")),
        "ev-computed-cash": ("Cash deposits", rows(lambda t: t.category == "cash_deposit")),
    }
    for ev_id, (description, derived) in specs.items():
        if derived:
            first = next(t for t in case.transactions if t.evidence_id == derived[0])
            case.evidence[ev_id] = Evidence(evidence_id=ev_id, doc_id=doc_id, page=first.page, kind="COMPUTED",
                                            description=description, derived_from=derived)


def cashflow_signals(case: Case) -> list[Signal]:
    f = case.features
    out: list[Signal] = []
    if f["nach_returns_6m"] >= 1 and "ev-computed-returns" in case.evidence:
        out.append(Signal(signal_id="sig-rk-nach", code="RK-NACH-RETURNS", category="RISK",
                          severity="HIGH" if f["nach_returns_3m"] >= 2 else "MEDIUM", confidence=0.95, detector="RULE",
                          corroborated=True, evidence_ids=["ev-computed-returns"],
                          title="Loan instalments returned unpaid",
                          explanation=f"{int(f['nach_returns_6m'])} instalment(s) bounced in six months, "
                                      f"{int(f['nach_returns_3m'])} in the last three."))
    if f["low_balance_days_share"] >= 0.25:
        out.append(Signal(signal_id="sig-rk-lowbal", code="RK-LOW-BALANCE", category="RISK", severity="MEDIUM",
                          confidence=0.9, detector="RULE", corroborated=True, evidence_ids=["ev-computed-balance"],
                          title="Balance often close to zero",
                          explanation=f"Balance was very low on {f['low_balance_days_share']:.0%} of days."))
    if f["digital_share"] >= 0.7 and "ev-computed-inflow" in case.evidence:
        out.append(Signal(signal_id="sig-pos-digital", code="POS-DIGITAL-INFLOWS", category="POSITIVE", severity="INFO",
                          confidence=0.95, detector="RULE", corroborated=True, evidence_ids=["ev-computed-inflow"],
                          title="Mostly digital, traceable income",
                          explanation=f"{f['digital_share']:.0%} of business inflows arrive through UPI, bank transfer "
                                      "or card settlement."))
    if f["inflow_cv"] <= 0.2 and "ev-computed-inflow" in case.evidence:
        out.append(Signal(signal_id="sig-pos-stable", code="POS-STABLE-INFLOWS", category="POSITIVE", severity="INFO",
                          confidence=0.9, detector="RULE", corroborated=True, evidence_ids=["ev-computed-inflow"],
                          title="Steady monthly income",
                          explanation=f"Monthly inflows vary by only {f['inflow_cv']:.0%} around the average."))
    return out
