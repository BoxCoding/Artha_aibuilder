"""Turn a parsed statement into domain documents, transactions and evidence."""
from __future__ import annotations

import hashlib

from app.documents.pdf_reader import ParsedStatement
from app.domain.models import Case, Document, Evidence, StatementHeader, Transaction
from app.security.injection import detect


def attach_parsed_statement(case: Case, doc_id: str, filename: str, data: bytes, parsed: ParsedStatement,
                            parser: str) -> Document:
    doc = Document(
        doc_id=doc_id, filename=filename, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
        page_count=parsed.page_count, parser=parser,  # type: ignore[arg-type]
        header=StatementHeader(bank_name=parsed.bank_name, account_holder=parsed.account_holder,
                               account_last4=parsed.account_last4, period_from=parsed.period_from,
                               period_to=parsed.period_to, opening_balance=parsed.opening_balance),
        metadata=parsed.metadata, incremental_updates=parsed.incremental_updates,
    )
    case.documents.append(doc)
    if parsed.header_bbox:
        ev = f"ev-{doc_id}-header"
        case.evidence[ev] = Evidence(evidence_id=ev, doc_id=doc_id, page=1, bbox=parsed.header_bbox, kind="HEADER",
                                     description="Statement header: bank, account holder, account and period")
    for row in parsed.rows:
        ev = f"ev-{doc_id}-p{row.page}-r{row.row_index}"
        case.evidence[ev] = Evidence(
            evidence_id=ev, doc_id=doc_id, page=row.page, bbox=row.row_bbox, kind="TXN_ROW",
            description=f"{row.txn_date:%d %b %Y} row on page {row.page}")
        case.transactions.append(Transaction(
            txn_id=f"t-{doc_id}-{row.row_index}", doc_id=doc_id, page=row.page, row_index=row.row_index,
            txn_date=row.txn_date, narration=row.narration, reference=row.reference, debit=row.debit,
            credit=row.credit, balance=row.balance, evidence_id=ev, amount_font=row.amount_font,
            amount_size=row.amount_size, amount_bbox=row.amount_bbox, redacted=bool(detect(row.narration)),
        ))
    return doc
