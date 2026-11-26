"""Read structured statement data: Account Aggregator FI data: the decrypted ReBIT deposit-account JSON an AA (via Setu, Finvu, OneMoney
and the like) returns after the customer's consent. It comes straight from the bank, so there is no layout
to read and nothing to OCR: every transaction carries its own type, amount and running balance.

Key case and the XML-to-JSON '@' prefix vary between providers, so keys are compared case-insensitively.

Also a Perfios statement report: Perfios's own analysis JSON (often built from the same AA data), of which we
take only the transactions and recompute everything else, so the score never rests on a vendor's summary.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.documents.pdf_reader import ParsedRow, ParsedStatement


def is_account_aggregator(data: bytes) -> bool:
    """Any JSON upload: Account Aggregator data or a Perfios report (`read_json` tells them apart)."""
    return data.lstrip()[:1] == b"{"


def read_json(data: bytes) -> tuple[ParsedStatement, str]:
    """(statement, parser name) for a JSON upload."""
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return ParsedStatement(page_count=0, needs_ai=True, reason="unreadable"), "account_aggregator"
    if isinstance(payload, dict) and "accountXns" in payload:
        return read_perfios(payload), "perfios"
    return read_account_aggregator(data), "account_aggregator"


def read_perfios(payload: dict[str, Any]) -> ParsedStatement:
    result = ParsedStatement(page_count=0, needs_ai=False, metadata={"Source": "Perfios statement report"})
    # shortcut: the busiest account only, as for AA data; upload a report per account to analyse several.
    accounts = sorted((a for a in _list(payload.get("accountXns")) if isinstance(a, dict)),
                      key=lambda a: -len(_list(a.get("xns"))))
    if not accounts:
        result.needs_ai, result.reason = True, "no_transactions_found"
        return result
    summary = payload.get("summaryInfo") or {}
    customer = payload.get("customerInfo") or {}
    result.bank_name = str(summary.get("instName") or customer.get("bank") or "").split(",")[0][:60] or None
    result.account_holder = str(customer.get("name") or "")[:80] or None
    number = str(accounts[0].get("accountNo") or summary.get("accNo") or "")
    result.account_last4 = number[-4:] if number[-4:].isdigit() else None
    rows: list[tuple[date, int, Decimal, Decimal, Decimal, str, str]] = []
    for n, txn in enumerate(_list(accounts[0].get("xns"))):
        try:
            amount = Decimal(str(txn["amount"]))  # signed: negative is a withdrawal
            balance = Decimal(str(txn["balance"]))
            day = date.fromisoformat(str(txn["date"])[:10])
        except (KeyError, TypeError, InvalidOperation, ValueError):
            continue
        rows.append((day, n, -amount if amount < 0 else Decimal("0"), amount if amount > 0 else Decimal("0"), balance,
                     str(txn.get("narration") or ""), str(txn.get("chqNo") or "")))
    return _finish(result, rows, None, None)


def _k(d: dict[str, Any], key: str) -> Any:
    return next((v for k, v in d.items() if k.lstrip("@_").lower() == key.lower()), None)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value] if value is not None else []


def _accounts(node: Any, fip: str | None = None) -> list[tuple[dict[str, Any], str | None]]:
    """Every deposit account in the payload, with the nearest FIP (bank) name above it."""
    found: list[tuple[dict[str, Any], str | None]] = []
    if isinstance(node, dict):
        fip = _k(node, "fipName") or _k(node, "fipId") or fip
        account = _k(node, "account")
        if isinstance(account, dict) and isinstance(_k(account, "transactions"), dict):
            found.append((account, fip))
        for value in node.values():
            found += _accounts(value, fip)
    elif isinstance(node, list):
        for value in node:
            found += _accounts(value, fip)
    return found


def _day(txn: dict[str, Any]) -> date | None:
    for key in ("transactionTimestamp", "valueDate"):
        raw = _k(txn, key)
        if isinstance(raw, str) and raw:
            try:
                return datetime.fromisoformat(raw.strip()).date()
            except ValueError:
                continue
    return None


def read_account_aggregator(data: bytes) -> ParsedStatement:
    result = ParsedStatement(page_count=0, needs_ai=False, metadata={"Source": "Account Aggregator"})
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        result.needs_ai, result.reason = True, "unreadable"
        return result
    # shortcut: a payload with several accounts is read as its busiest one; upload each account separately
    # to have all of them analysed.
    accounts = sorted(_accounts(payload), key=lambda a: -len(_list(_k(_k(a[0], "transactions"), "transaction"))))
    if not accounts:
        result.needs_ai, result.reason = True, "no_transactions_found"
        return result
    account, fip = accounts[0]
    holders = _list(_k(_k(_k(account, "profile") or {}, "holders") or {}, "holder"))
    result.bank_name = str(fip)[:60] if fip else None
    result.account_holder = next((str(_k(h, "name")) for h in holders if isinstance(h, dict) and _k(h, "name")), None)
    masked = str(_k(account, "maskedAccNumber") or "")
    result.account_last4 = masked[-4:] if masked[-4:].isdigit() else None

    rows: list[tuple[date, int, Decimal, Decimal, Decimal, str, str]] = []
    for n, txn in enumerate(_list(_k(_k(account, "transactions"), "transaction"))):
        kind = str(_k(txn, "type") or "").upper()
        day = _day(txn)
        try:
            amount = abs(Decimal(str(_k(txn, "amount"))))
            balance = Decimal(str(_k(txn, "currentBalance")))
        except (InvalidOperation, ValueError):
            continue
        if kind not in ("DEBIT", "CREDIT") or day is None:
            continue
        rows.append((day, n, amount if kind == "DEBIT" else Decimal("0"), amount if kind == "CREDIT" else Decimal("0"),
                     balance, str(_k(txn, "narration") or ""), str(_k(txn, "reference") or _k(txn, "txnId") or "")))
    period = _k(account, "transactions")
    return _finish(result, rows, _parse(_k(period, "startDate")), _parse(_k(period, "endDate")))


def _finish(result: ParsedStatement, rows: list[tuple[date, int, Decimal, Decimal, Decimal, str, str]],
            period_from: date | None, period_to: date | None) -> ParsedStatement:
    if not rows:
        result.needs_ai, result.reason = True, "no_transactions_found"
        return result
    if rows[0][0] > rows[-1][0]:
        rows.reverse()  # newest-first feed: flip it whole, so the order within each day flips too
    rows.sort(key=lambda r: r[0])  # stable: keeps the bank's order within a day
    result.rows = [ParsedRow(page=1, row_index=i, txn_date=day, narration=narration[:240], reference=ref[:60],
                             debit=debit, credit=credit, balance=balance, row_bbox=[], amount_bbox=None,
                             amount_font=None, amount_size=None, balance_font=None, balance_size=None)
                   for i, (day, _n, debit, credit, balance, narration, ref) in enumerate(rows)]
    first = result.rows[0]
    result.opening_balance = first.balance - first.credit + first.debit
    result.period_from = period_from or result.rows[0].txn_date
    result.period_to = period_to or result.rows[-1].txn_date
    return result


def _parse(raw: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(raw)).date() if raw else None
    except ValueError:
        return None
