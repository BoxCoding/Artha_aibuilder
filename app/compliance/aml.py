"""Anti-money-laundering screening: names against a watchlist, and reportable cash.

* Watchlist: the applicant, the business, and every counterparty named in the statement narrations
  are compared with the watchlist (token matching that tolerates word order, titles and suffixes).
  A hit is a COMPLIANCE finding: the case is referred and cannot be approved until compliance clears it.
* Reportable cash: under the PMLA rules a reporting entity reports cash transactions of ₹10 lakh or more
  in a month. Months at or above the threshold are flagged for the compliance team, not held against
  the applicant.

This screens against whatever list is configured. It is not a substitute for a licensed screening
service; it is the integration point for one.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.domain.models import Case, Evidence, Signal

WATCHLIST = Path(__file__).with_name("watchlist.csv")
_NOISE = {"mr", "mrs", "ms", "shri", "smt", "the", "and", "pvt", "private", "ltd", "limited", "llp", "co", "company",
          "enterprises", "traders", "trading", "stores", "services", "agents", "exchange", "upi", "neft", "imps", "rtgs",
          "cr", "dr", "payment", "transfer", "to", "from", "by", "self", "cash", "deposit", "branch"}


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in re.split(r"[^a-z]+", text.lower()) if len(t) > 2 and t not in _NOISE)


@lru_cache
def watchlist() -> list[tuple[str, str, frozenset[str]]]:
    rows = []
    with WATCHLIST.open(encoding="utf-8") as f:
        for row in csv.DictReader(line for line in f if not line.startswith("#")):
            tokens = _tokens(row["name"])
            if tokens:
                rows.append((row["name"], row["list"], tokens))
    return rows


def match(name: str) -> tuple[str, str] | None:
    """A watchlist entry whose distinctive words all appear in `name`."""
    have = _tokens(name)
    for entry, lst, tokens in watchlist():
        if tokens <= have:
            return entry, lst
    return None


def counterparties(narration: str) -> list[str]:
    """Names inside a narration: 'UPI/CR/412233/SHARMA TRADERS/Payment' -> ['SHARMA TRADERS', 'Payment']."""
    parts = re.split(r"[/|-]", narration)
    return [p.strip() for p in parts if re.search(r"[A-Za-z]{3,}", p) and not re.fullmatch(r"[A-Z]{2,5}\d*", p.strip())]


def aml_signals(case: Case) -> list[Signal]:
    signals: list[Signal] = []
    applicant = case.applicant
    header_ev = next((f"ev-{d.doc_id}-header" for d in case.documents if f"ev-{d.doc_id}-header" in case.evidence), None)
    if applicant:
        for field, value in (("applicant", applicant.full_name), ("business", applicant.business_name)):
            hit = match(value)
            if hit:
                ev = header_ev or "ev-kyc"
                case.evidence.setdefault(ev, Evidence(evidence_id=ev, doc_id=case.documents[0].doc_id if case.documents else "none",
                                                      page=1, kind="COMPUTED", description="Applicant details (KYC)"))
                signals.append(Signal(
                    signal_id=f"sig-aml-san-001-{field}", code="AML-SAN-001", category="COMPLIANCE", severity="HIGH",
                    confidence=0.8, evidence_ids=[ev],
                    title=f"The {field} name matches a watchlist entry",
                    explanation=f"'{value[:60]}' matches '{hit[0]}' on the {hit[1]} list. Compliance must review "
                                "and clear this before the loan can be approved."))
    hits: dict[str, list[str]] = defaultdict(list)
    for t in case.transactions:
        if t.redacted:
            continue
        for name in counterparties(t.narration):
            found = match(name)
            if found:
                hits[f"{found[0]} ({found[1]})"].append(t.evidence_id)
    for n, (entry, evidence) in enumerate(sorted(hits.items())):
        signals.append(Signal(
            signal_id=f"sig-aml-san-002-{n}", code="AML-SAN-002", category="COMPLIANCE", severity="HIGH",
            confidence=0.7, evidence_ids=evidence[:10],
            title="A counterparty in the statement matches a watchlist entry",
            explanation=f"{len(evidence)} transaction(s) name a counterparty matching {entry}. Compliance must "
                        "review the relationship before approval."))

    threshold = Decimal(get_settings().cash_reporting_threshold)
    monthly: dict[str, list] = defaultdict(list)
    for t in case.transactions:
        if t.category == "cash_deposit" and t.credit > 0:
            monthly[t.txn_date.strftime("%Y-%m")].append(t)
    for month, rows in sorted(monthly.items()):
        total = sum((r.credit for r in rows), Decimal("0"))
        if total >= threshold:
            signals.append(Signal(
                signal_id=f"sig-aml-ctr-001-{month}", code="AML-CTR-001", category="COMPLIANCE", severity="MEDIUM",
                confidence=0.95, evidence_ids=[r.evidence_id for r in rows][:10],
                title=f"Reportable cash deposits in {month}",
                explanation=f"Cash deposits of ₹{total:,.0f} in {month} meet the ₹{threshold:,.0f} monthly reporting "
                            "threshold. This is a reporting duty for the lender, not a finding against the applicant."))
    return signals
