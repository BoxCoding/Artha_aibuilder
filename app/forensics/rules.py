"""Deterministic document-integrity rules.

Each rule is a pure function of the case's documents, transactions and evidence, returning
signals that always carry evidence IDs. Rules never call a model. Visual (LLM) findings are
added separately and can only escalate a case when corroborated by one of these rules.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from app.domain.models import Case, Document, Evidence, Signal, Transaction
from app.security.injection import detect

TOLERANCE = Decimal("0.01")
# Statement systems we know each bank uses. A producer that differs is only meaningful when we know
# what to expect: real banks use many generators (iText, BI Publisher, JasperReports, browser print),
# so an unknown bank's producer is never, by itself, a finding. Extend as real statements are profiled.
EXPECTED_PRODUCERS_BY_BANK: dict[str, tuple[str, ...]] = {
    "kaveri commercial bank": ("corebanking statement engine",),  # the synthetic generator's bank
}
EDITOR_HINTS = ("editor", "ilovepdf", "smallpdf", "sejda", "pdfescape", "foxit phantom", "acrobat",
                "nitro", "canva", "microsoft word", "libreoffice", "quickpdf", "pdf24", "photoshop")

Rule = Callable[[Case], list[Signal]]


def _sid(case: Case, code: str, n: int = 0) -> str:
    return f"sig-{code.lower()}-{n}"


def _txns_by_doc(case: Case) -> dict[str, list[Transaction]]:
    grouped: dict[str, list[Transaction]] = defaultdict(list)
    for t in case.transactions:
        grouped[t.doc_id].append(t)
    for rows in grouped.values():
        rows.sort(key=lambda t: t.row_index)
    return grouped


def _doc(case: Case, doc_id: str) -> Document:
    return next(d for d in case.documents if d.doc_id == doc_id)


def reconcile(case: Case) -> dict[str, list[tuple[Transaction, Transaction | None, Decimal]]]:
    """Return per-document balance breaks as (row, previous_row, difference)."""
    breaks: dict[str, list[tuple[Transaction, Transaction | None, Decimal]]] = {}
    for doc_id, rows in _txns_by_doc(case).items():
        doc = _doc(case, doc_id)
        prev_balance = doc.header.opening_balance
        prev_row: Transaction | None = None
        found: list[tuple[Transaction, Transaction | None, Decimal]] = []
        for row in rows:
            if prev_balance is not None:
                expected = prev_balance + row.credit - row.debit
                diff = row.balance - expected
                if abs(diff) > TOLERANCE:
                    found.append((row, prev_row, diff))
            prev_balance, prev_row = row.balance, row  # re-anchor so one edit is not counted many times
        breaks[doc_id] = found
    return breaks


def rule_balance_breaks(case: Case) -> list[Signal]:
    signals: list[Signal] = []
    font_rows = {t.txn_id for t in _font_outliers(case)}
    for doc_id, found in reconcile(case).items():
        if not found:
            continue
        doc = _doc(case, doc_id)
        evidence_ids: list[str] = []
        for row, prev, _diff in found[:8]:
            if prev:
                evidence_ids.append(prev.evidence_id)
            evidence_ids.append(row.evidence_id)
        coincides = any(row.txn_id in font_rows or (prev and prev.txn_id in font_rows) for row, prev, _ in found)
        text_layer = doc.parser == "template"
        severity = "CRITICAL" if (len(found) >= 2 or coincides) and text_layer else ("HIGH" if text_layer else "MEDIUM")
        first_row, _, first_diff = found[0]
        signals.append(Signal(
            signal_id=_sid(case, "FR-BAL-001"), code="FR-BAL-001", category="FRAUD", severity=severity,
            confidence=0.97 if text_layer else 0.6, detector="RULE", corroborated=text_layer,
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            title="Running balance does not add up",
            explanation=(
                f"{len(found)} row(s) where the previous balance plus deposits minus withdrawals does not equal "
                f"the printed balance; first break on page {first_row.page} ({first_row.txn_date:%d %b %Y}), "
                f"off by {abs(first_diff):,.2f}."
                + ("" if text_layer else " The statement was read from an image, so this may be a reading error.")
            ),
        ))
    return signals


def _font_outliers(case: Case) -> list[Transaction]:
    outliers: list[Transaction] = []
    for rows in _txns_by_doc(case).values():
        metrics = Counter((t.amount_font, t.amount_size) for t in rows if t.amount_font)
        if len(metrics) < 2:
            continue
        (mode_font, mode_size), mode_count = metrics.most_common(1)[0]
        if mode_count / max(1, sum(metrics.values())) < 0.8 or mode_size is None:
            continue
        for t in rows:
            if t.amount_font and (t.amount_font != mode_font or abs((t.amount_size or 0) - mode_size) > 0.3):
                outliers.append(t)
    return outliers


def rule_font_inconsistency(case: Case) -> list[Signal]:
    outliers = _font_outliers(case)
    if not outliers:
        return []
    evidence_ids: list[str] = []
    for t in outliers[:10]:
        cell_id = f"{t.evidence_id}-amount"
        if cell_id not in case.evidence and t.amount_bbox:
            case.evidence[cell_id] = Evidence(
                evidence_id=cell_id, doc_id=t.doc_id, page=t.page, bbox=t.amount_bbox, kind="CELL",
                description=f"Amount cell printed in {t.amount_font} {t.amount_size}pt", derived_from=[t.evidence_id])
        evidence_ids.append(cell_id if cell_id in case.evidence else t.evidence_id)
    large = sum(1 for t in outliers if t.credit >= Decimal("50000"))
    return [Signal(
        signal_id=_sid(case, "FR-FONT-001"), code="FR-FONT-001", category="FRAUD",
        severity="CRITICAL" if large >= 2 else "HIGH", confidence=0.9, detector="RULE", corroborated=True,
        evidence_ids=evidence_ids, title="Amounts printed in a different font",
        explanation=(f"{len(outliers)} amount cell(s) use a font or size that differs from every other amount in the "
                     f"column; {large} of them are deposits of 50,000 or more. Edited numbers often show this pattern."),
    )]


def rule_editing_traces(case: Case) -> list[Signal]:
    signals: list[Signal] = []
    for n, doc in enumerate(case.documents):
        producer = (doc.metadata.get("Producer", "") + " " + doc.metadata.get("Creator", "")).lower()
        editor = next((hint for hint in EDITOR_HINTS if hint in producer), None)
        expected = EXPECTED_PRODUCERS_BY_BANK.get((doc.header.bank_name or "").strip().lower())
        unexpected = bool(expected) and bool(producer.strip()) and not any(p in producer for p in expected)
        created, modified = doc.metadata.get("CreationDate", ""), doc.metadata.get("ModDate", "")
        later_edit = bool(created and modified and modified[2:10] > created[2:10])
        reasons = []
        if editor:
            reasons.append(f"producer field names editing software ({doc.metadata.get('Producer', '')[:60]})")
        elif unexpected:
            reasons.append("producer differs from the issuing bank's statement system")
        if doc.incremental_updates:
            reasons.append(f"file was re-saved {doc.incremental_updates} time(s) after it was issued")
        if later_edit:
            reasons.append("modification date is later than the creation date")
        if not reasons:
            continue
        ev_id = f"ev-{doc.doc_id}-metadata"
        case.evidence[ev_id] = Evidence(evidence_id=ev_id, doc_id=doc.doc_id, page=1, kind="METADATA",
                                        description="PDF document properties: " + "; ".join(reasons))
        severity = "HIGH" if editor and (doc.incremental_updates or later_edit) else "MEDIUM"
        signals.append(Signal(
            signal_id=_sid(case, "FR-META-001", n), code="FR-META-001", category="FRAUD", severity=severity,
            confidence=0.8 if severity == "HIGH" else 0.55, detector="RULE", corroborated=True,
            evidence_ids=[ev_id], title="File shows signs of editing after issue",
            explanation="The " + "; the ".join(reasons) + ". Editing traces alone can be innocent, so this needs corroboration.",
        ))
    return signals


def rule_injection(case: Case) -> list[Signal]:
    hits = [t for t in case.transactions if t.redacted or detect(t.narration)]
    if not hits:
        return []
    return [Signal(
        signal_id=_sid(case, "FR-INJ-001"), code="FR-INJ-001", category="FRAUD", severity="HIGH", confidence=0.95,
        detector="RULE", corroborated=True, evidence_ids=[t.evidence_id for t in hits[:5]],
        title="Hidden instructions aimed at the AI",
        explanation=(f"{len(hits)} transaction description(s) contain text addressed to an automated reviewer. "
                     "The text was removed before any model read the document and had no effect on the assessment."),
    )]


def rule_duplicate_references(case: Case) -> list[Signal]:
    refs = Counter(t.reference for t in case.transactions if len(t.reference) >= 6)
    dupes = [t for t in case.transactions if refs[t.reference] > 1]
    if not dupes:
        return []
    return [Signal(
        signal_id=_sid(case, "FR-TXN-001"), code="FR-TXN-001", category="FRAUD", severity="MEDIUM", confidence=0.7,
        detector="RULE", corroborated=True, evidence_ids=[t.evidence_id for t in dupes[:6]],
        title="Repeated transaction reference numbers",
        explanation=f"{len(dupes)} rows share a reference number with another row. Bank references are normally unique.",
    )]


def rule_date_sequence(case: Case) -> list[Signal]:
    bad: list[Transaction] = []
    for doc_id, rows in _txns_by_doc(case).items():
        header = _doc(case, doc_id).header
        for prev, row in zip(rows, rows[1:]):
            if row.txn_date < prev.txn_date:
                bad.append(row)
        if header.period_from and header.period_to:
            bad.extend(t for t in rows if not header.period_from <= t.txn_date <= header.period_to)
    if not bad:
        return []
    return [Signal(
        signal_id=_sid(case, "FR-TXN-004"), code="FR-TXN-004", category="FRAUD", severity="HIGH", confidence=0.85,
        detector="RULE", corroborated=True, evidence_ids=list(dict.fromkeys(t.evidence_id for t in bad[:6])),
        title="Dates out of order or outside the statement period",
        explanation=f"{len(bad)} row(s) are dated out of sequence or outside the period printed on the statement.",
    )]


def rule_window_dressing(case: Case) -> list[Signal]:
    if not case.transactions:
        return []
    end = max(t.txn_date for t in case.transactions)
    recent = [t for t in case.transactions if t.category == "cash_deposit" and t.credit >= Decimal("50000")
              and t.credit % Decimal("10000") == 0 and t.txn_date >= end - timedelta(days=15)]
    if not recent:
        return []
    total = sum((t.credit for t in recent), Decimal("0"))
    return [Signal(
        signal_id=_sid(case, "FR-TXN-002"), code="FR-TXN-002", category="RISK", severity="MEDIUM", confidence=0.65,
        detector="RULE", corroborated=True, evidence_ids=[t.evidence_id for t in recent],
        title="Large round cash deposits just before applying",
        explanation=(f"{len(recent)} round-figure cash deposit(s) totalling {total:,.0f} in the final 15 days of the "
                     "statement can temporarily lift the average balance."),
    )]


def rule_statistical_outliers(case: Case) -> list[Signal]:
    """Deposits that do not fit the account's own pattern.

    Font and metadata traces disappear if a forger rebuilds the document from scratch. Invented
    numbers do not: a deposit typed in to lift reported income is usually far outside the
    distribution of that account's other deposits in the same category. Compared within category
    (a cash deposit is not a bank transfer) on a log scale with a robust z-score, and raised as a
    MEDIUM verification trigger rather than a fraud accusation, since real businesses do get the
    occasional genuinely large payment.
    """
    from collections import defaultdict as _dd
    from math import log10
    from statistics import median

    from app.cashflow.categorise import BUSINESS_CREDIT

    by_category: dict[str, list[Transaction]] = _dd(list)
    for t in case.transactions:
        if t.credit > 0 and t.category in BUSINESS_CREDIT:
            by_category[t.category].append(t)

    flagged: list[Transaction] = []
    total_credit = sum((t.credit for rows in by_category.values() for t in rows), Decimal("0"))
    for rows in by_category.values():
        if len(rows) < 20:
            continue  # too few to characterise a distribution
        logs = [log10(float(t.credit)) for t in rows]
        centre = median(logs)
        deviations = [abs(v - centre) for v in logs]
        mad = median(deviations)
        if mad <= 0:
            continue
        for t, value in zip(rows, logs):
            if 0.6745 * (value - centre) / mad > 3.5:
                flagged.append(t)

    if not flagged or not total_credit:
        return []
    share = sum((t.credit for t in flagged), Decimal("0")) / total_credit
    if share < Decimal("0.04"):
        return []  # immaterial to the income picture, so not worth an underwriter's time
    return [Signal(
        signal_id=_sid(case, "FR-STAT-001"), code="FR-STAT-001", category="FRAUD", severity="MEDIUM",
        confidence=0.6, detector="RULE", corroborated=True,
        evidence_ids=[t.evidence_id for t in flagged[:8]],
        title="Deposits that do not fit this account's pattern",
        explanation=(f"{len(flagged)} deposit(s), {share:.0%} of total business inflow, are far larger than every "
                     "other deposit of the same type in this account. That can be genuine, so it needs "
                     "verification rather than assumption."),
    )]


def rule_circular_transfers(case: Case) -> list[Signal]:
    credits = [t for t in case.transactions if t.credit > 0 and "/" in t.narration]
    debits = [t for t in case.transactions if t.debit > 0 and "/" in t.narration]
    pairs: list[str] = []
    for c in credits:
        party = c.narration.split("/")[-1].strip()
        if len(party) < 4:
            continue
        for d in debits:
            if (d.narration.split("/")[-1].strip() == party and abs((d.txn_date - c.txn_date).days) <= 3
                    and abs(d.debit - c.credit) <= c.credit * Decimal("0.02") and c.credit >= Decimal("20000")):
                pairs.extend([c.evidence_id, d.evidence_id])
    if not pairs:
        return []
    return [Signal(
        signal_id=_sid(case, "FR-TXN-003"), code="FR-TXN-003", category="FRAUD", severity="MEDIUM", confidence=0.6,
        detector="RULE", corroborated=True, evidence_ids=list(dict.fromkeys(pairs))[:8],
        title="Money sent back to the same party within days",
        explanation="Similar amounts were received from and paid back to the same counterparty within three days.",
    )]


RULES: tuple[Rule, ...] = (
    rule_injection, rule_balance_breaks, rule_font_inconsistency, rule_editing_traces,
    rule_duplicate_references, rule_date_sequence, rule_circular_transfers, rule_window_dressing,
    rule_statistical_outliers,
)


def rule_identity_consistency(case: Case) -> list[Signal]:
    """The statement should belong to the applicant or their business. A mismatch is a reason to
    verify, not an accusation: family accounts and trade names are common and innocent."""
    from app.domain.kyc import names_match

    applicant = case.applicant
    if applicant is None:
        return []
    signals: list[Signal] = []
    for n, doc in enumerate(case.documents):
        holder = (doc.header.account_holder or "").strip()
        if not holder or names_match(holder, applicant.full_name, applicant.business_name):
            continue
        ev_id = f"ev-{doc.doc_id}-header"
        if ev_id not in case.evidence:
            ev_id = f"ev-{doc.doc_id}-holder"
            case.evidence[ev_id] = Evidence(evidence_id=ev_id, doc_id=doc.doc_id, page=1, kind="HEADER",
                                            description=f"Account holder printed on the statement: {holder[:60]}")
        signals.append(Signal(
            signal_id=_sid(case, "ID-NAME-001", n), code="ID-NAME-001", category="FRAUD", severity="MEDIUM",
            confidence=0.6, detector="RULE", corroborated=True, evidence_ids=[ev_id],
            title="Statement is in a different name",
            explanation=(f"The account holder on the statement ({holder[:60]}) does not match the applicant or the "
                         "business named in the application. Confirm the account belongs to the applicant."),
        ))
    return signals


RULES = RULES + (rule_identity_consistency,)


def run_rules(case: Case) -> list[Signal]:
    signals: list[Signal] = []
    for rule in RULES:
        signals.extend(rule(case))
    return signals
