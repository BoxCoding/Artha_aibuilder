"""Gemini-powered document steps: extraction for unknown layouts/scans, narration categorisation,
and visual inspection of suspicious cells. All outputs are validated and converted to domain objects."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from app.ai.gateway import Blob, LLMGateway, UsageMeter
from app.ai.schemas import CategoryBatch, StatementExtraction, VisualReport
from app.documents.pdf_reader import ParsedRow, ParsedStatement, inspect_structure
from app.documents.render import render_crop
from app.domain.models import Case, Signal, Transaction
from app.security.injection import REDACTED, detect
from app.security.masking import mask_text


def _dec(value: str | None) -> Decimal:
    if not value:
        return Decimal("0")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


async def extract_with_gemini(data: bytes, gateway: LLMGateway, meter: UsageMeter) -> ParsedStatement:
    """Model extraction for layouts the deterministic readers cannot place (any configured provider)."""
    meta, incremental, has_js, encrypted, pages = inspect_structure(data)
    result = await gateway.structured(
        prompt_id="doc_intel", schema=StatementExtraction, meter=meter,
        contents=[Blob(data, "application/pdf"), "Extract this bank statement."])
    parsed = ParsedStatement(page_count=pages, needs_ai=False, metadata=meta, incremental_updates=incremental,
                             has_javascript=has_js, encrypted=encrypted, bank_name=result.bank_name,
                             account_holder=result.account_holder,
                             account_last4=(result.account_last4 or "")[-4:] or None,
                             period_from=_date(result.period_from), period_to=_date(result.period_to),
                             opening_balance=_dec(result.opening_balance) if result.opening_balance else None)
    for i, t in enumerate(result.transactions):
        d = _date(t.date)
        if d is None or not t.balance:
            continue
        box = [max(0, min(1000, int(v))) for v in t.box_2d[:4]] if len(t.box_2d) >= 4 else None
        parsed.rows.append(ParsedRow(page=max(1, t.page), row_index=i, txn_date=d, narration=(t.narration or "")[:120],
                                     reference=(t.reference or "")[:30], debit=_dec(t.debit), credit=_dec(t.credit),
                                     balance=_dec(t.balance), row_bbox=box or [0, 0, 0, 0], amount_bbox=None,
                                     amount_font=None, amount_size=None, balance_font=None, balance_size=None))
    if not parsed.rows:
        parsed.needs_ai, parsed.reason = True, "model_found_no_transactions"
    return parsed


async def categorise_with_gemini(unmatched: list[Transaction], gateway: LLMGateway, meter: UsageMeter) -> int:
    if not unmatched:
        return 0
    lookup = {t.txn_id: t for t in unmatched}
    updated = 0
    for start in range(0, len(unmatched), 60):
        batch = unmatched[start:start + 60]
        items = [{"txn_id": t.txn_id, "direction": "credit" if t.credit > 0 else "debit",
                  "narration": REDACTED if (t.redacted or detect(t.narration)) else mask_text(t.narration)}
                 for t in batch]
        result = await gateway.structured(prompt_id="categorise", schema=CategoryBatch, meter=meter,
                                          model=gateway.settings.model_lite, contents=[json.dumps(items)])
        for item in result.items:
            txn = lookup.get(item.txn_id)
            if txn and item.confidence != "low" and (item.category.endswith("credit") or txn.debit > 0
                                                     or item.category in ("cash_deposit", "nach_return", "loan_disbursal")):
                txn.category, txn.category_source = item.category, "LLM"
                updated += 1
    return updated


async def visual_inspection(case: Case, pdf_by_doc: dict[str, bytes], gateway: LLMGateway,
                            meter: UsageMeter, max_crops: int = 6) -> list[Signal]:
    """Crop suspicious amount cells plus a few large credits and ask a vision model for editing cues."""
    rule_rows = {e for s in case.signals if s.detector == "RULE" and s.category == "FRAUD" for e in s.evidence_ids}
    large = sorted((t for t in case.transactions if t.amount_bbox), key=lambda t: -t.credit)[:3]
    picks: list[Transaction] = []
    for t in case.transactions:
        if (t.evidence_id in rule_rows or f"{t.evidence_id}-amount" in rule_rows) and t.amount_bbox:
            picks.append(t)
    picks += [t for t in large if t not in picks]
    picks = picks[:max_crops]
    if not picks:
        return []
    contents: list[object] = []
    for t in picks:
        row = case.evidence[t.evidence_id].bbox or t.amount_bbox or [0, 0, 1000, 1000]
        png = render_crop(pdf_by_doc[t.doc_id], t.page, row)
        flagged = t.evidence_id in rule_rows or f"{t.evidence_id}-amount" in rule_rows
        contents += [f"crop_id={t.txn_id}; reason={'rule finding' if flagged else 'large deposit'}",
                     Blob(png, "image/png")]
    report = await gateway.structured(prompt_id="visual_inspector", schema=VisualReport, meter=meter, contents=contents)
    by_id = {t.txn_id: t for t in picks}
    signals: list[Signal] = []
    for n, finding in enumerate(f for f in report.findings if f.confidence != "low"):
        txn = by_id.get(finding.crop_id)
        if not txn:
            continue
        corroborated = txn.evidence_id in rule_rows or f"{txn.evidence_id}-amount" in rule_rows
        signals.append(Signal(
            signal_id=f"sig-fr-img-001-{n}", code="FR-IMG-001", category="FRAUD",
            severity="MEDIUM" if corroborated else "LOW", confidence=0.75 if finding.confidence == "high" else 0.55,
            detector="LLM_VISUAL", corroborated=corroborated, evidence_ids=[txn.evidence_id],
            title=f"Visual editing cue ({gateway.provider})", explanation=finding.description[:380]))
    return signals
