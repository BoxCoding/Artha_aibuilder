"""Google Document AI for scanned and photographed statements.

When DOCUMENT_AI_PROCESSOR is set (a Form Parser or Bank Statement Parser processor), statements with no
text layer are sent to Document AI. Its detected tables go through the same column vocabulary as the
universal reader: header cells are classified (date, narration, debit, credit, amount, balance), body
cells are parsed as dates and amounts, and every row keeps its bounding box so evidence citations still
point at the right place on the page. The result must reconcile like any other reading.
"""
from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.documents.pdf_reader import ParsedRow, ParsedStatement, inspect_structure
from app.documents.universal_reader import _classify, parse_date, parse_money


def _text(doc: dict[str, Any], layout: dict[str, Any]) -> str:
    full = doc.get("text", "")
    segments = layout.get("textAnchor", {}).get("textSegments", [])
    return " ".join(full[int(s.get("startIndex", 0)):int(s["endIndex"])] for s in segments).strip()


def _box(layout: dict[str, Any]) -> list[int] | None:
    vertices = layout.get("boundingPoly", {}).get("normalizedVertices") or []
    if not vertices:
        return None
    xs = [float(v.get("x", 0)) for v in vertices]
    ys = [float(v.get("y", 0)) for v in vertices]
    return [round(min(ys) * 1000), round(min(xs) * 1000), round(max(ys) * 1000), round(max(xs) * 1000)]


def from_document(doc: dict[str, Any], data: bytes) -> ParsedStatement:
    meta, incremental, has_js, encrypted, pages = inspect_structure(data)
    result = ParsedStatement(page_count=pages or len(doc.get("pages", [])), needs_ai=False, metadata=meta,
                             incremental_updates=incremental, has_javascript=has_js, encrypted=encrypted)
    kinds: list[str | None] | None = None
    previous: Decimal | None = None
    for page_no, page in enumerate(doc.get("pages", []), start=1):
        for table in page.get("tables", []):
            for header in table.get("headerRows", [])[:1]:
                kinds = [_classify(_text(doc, c.get("layout", {}))) for c in header.get("cells", [])]
            if not kinds:
                continue
            for row in table.get("bodyRows", []):
                cells = row.get("cells", [])
                values = {kind: (_text(doc, c.get("layout", {})), c.get("layout", {}))
                          for kind, c in zip(kinds, cells) if kind}
                txn_date = parse_date(values.get("date", ("", {}))[0])
                balance = parse_money(values.get("balance", ("", {}))[0])
                if txn_date is None or balance is None:
                    continue
                bal = -balance[0] if balance[1] == "dr" else balance[0]
                debit = credit = Decimal("0")
                amount_layout: dict[str, Any] = {}
                for kind in ("debit", "credit"):
                    money = parse_money(values.get(kind, ("", {}))[0])
                    if money:
                        amount_layout = values[kind][1]
                        if kind == "debit":
                            debit = abs(money[0])
                        else:
                            credit = abs(money[0])
                if "amount" in values and not (debit or credit):
                    money = parse_money(values["amount"][0])
                    if money:
                        amount_layout = values["amount"][1]
                        marker = money[1] or (values.get("drcr", ("", {}))[0][:1].lower() + "r"
                                              if values.get("drcr") else None)
                        is_credit = marker == "cr" or (marker is None and previous is not None and bal >= previous)
                        credit, debit = (abs(money[0]), Decimal("0")) if is_credit else (Decimal("0"), abs(money[0]))
                previous = bal
                row_box = _box(row.get("cells", [{}])[0].get("layout", {})) or [0, 0, 0, 0]
                row_box = [row_box[0], 0, row_box[2], 1000]
                result.rows.append(ParsedRow(
                    page=page_no, row_index=len(result.rows), txn_date=txn_date,
                    narration=values.get("narration", ("", {}))[0][:240], reference=values.get("ref", ("", {}))[0][:60],
                    debit=debit, credit=credit, balance=bal, row_bbox=row_box, amount_bbox=_box(amount_layout),
                    amount_font=None, amount_size=None, balance_font=None, balance_size=None))
    if len(result.rows) >= 2 and result.rows[0].txn_date > result.rows[-1].txn_date:
        result.rows = list(reversed(result.rows))
        for i, r in enumerate(result.rows):
            r.row_index = i
    if not result.rows:
        result.needs_ai, result.reason = True, "document_ai_found_no_transactions"
    else:
        first = result.rows[0]
        result.opening_balance = first.balance - first.credit + first.debit
        result.period_from, result.period_to = result.rows[0].txn_date, result.rows[-1].txn_date
    return result


def extract_with_document_ai(data: bytes) -> ParsedStatement:
    from app.gcp import get_gcp

    processor = get_settings().document_ai_processor
    if not processor:
        raise RuntimeError("DOCUMENT_AI_PROCESSOR is not configured")
    location = processor.split("/locations/")[1].split("/")[0]
    response = get_gcp().call(
        "POST", f"https://{location}-documentai.googleapis.com/v1/{processor}:process",
        {"rawDocument": {"content": base64.b64encode(data).decode("ascii"), "mimeType": "application/pdf"},
         "skipHumanReview": True})
    return from_document(response.get("document", {}), data)
