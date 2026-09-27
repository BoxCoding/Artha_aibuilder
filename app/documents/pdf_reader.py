"""Deterministic reader for text-layer bank statements.

Extracts the header, transaction rows, per-cell font metrics and normalised bounding boxes
(evidence) from the PDF text layer. Statements without a usable text layer (scans, photos)
or in unknown layouts return `needs_ai=True` so the Gemini extractor can take over.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber
from pypdf import PdfReader

DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
AMOUNT_RE = re.compile(r"^-?\d{1,3}(,\d{2})*(,\d{3})?\.\d{2}$|^-?\d+\.\d{2}$")
# A reference and an amount printed in adjacent columns can touch, in which case the PDF text layer
# yields a single merged token ("6502374677091,00,340.00"). Split it back apart rather than dropping
# the amount, which would otherwise look like a balance break.
MERGED_RE = re.compile(r"^(?P<head>\d{6,})(?P<amount>\d{1,3}(?:,\d{2})*(?:,\d{3})?\.\d{2})$")

HEADER_KEYS = {"date": "Date", "narration": "Narration", "ref": "Ref", "debit": "Withdrawal",
               "credit": "Deposit", "balance": "Balance"}


@dataclass
class ParsedRow:
    page: int
    row_index: int
    txn_date: date
    narration: str
    reference: str
    debit: Decimal
    credit: Decimal
    balance: Decimal
    row_bbox: list[int]
    amount_bbox: list[int] | None
    amount_font: str | None
    amount_size: float | None
    balance_font: str | None
    balance_size: float | None


@dataclass
class ParsedStatement:
    page_count: int
    needs_ai: bool
    reason: str | None = None
    bank_name: str | None = None
    account_holder: str | None = None
    account_last4: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    opening_balance: Decimal | None = None
    header_bbox: list[int] | None = None
    rows: list[ParsedRow] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    incremental_updates: int = 0
    has_javascript: bool = False
    encrypted: bool = False


def parse_amount(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - guarded by AMOUNT_RE
        raise ValueError(f"not an amount: {text!r}") from exc


def norm_box(x0: float, top: float, x1: float, bottom: float, width: float, height: float) -> list[int]:
    """Return [ymin, xmin, ymax, xmax] normalised to 0-1000 (the convention Gemini uses for boxes)."""
    clamp = lambda v: max(0, min(1000, round(v)))  # noqa: E731
    return [clamp(top / height * 1000), clamp(x0 / width * 1000),
            clamp(bottom / height * 1000), clamp(x1 / width * 1000)]


def inspect_structure(data: bytes) -> tuple[dict[str, str], int, bool, bool, int]:
    """Metadata, incremental update count, JavaScript presence, encryption, page count."""
    reader = PdfReader(io.BytesIO(data))
    encrypted = bool(reader.is_encrypted)
    meta: dict[str, str] = {}
    if not encrypted and reader.metadata:
        for key, value in reader.metadata.items():
            meta[str(key).lstrip("/")] = str(value)[:200]
    # Each incremental save appends another trailer/startxref section.
    incremental = max(0, data.count(b"startxref") - 1)
    has_js = b"/JavaScript" in data or b"/JS " in data or b"/Launch" in data
    pages = 0 if encrypted else len(reader.pages)
    return meta, incremental, has_js, encrypted, pages


def read_statement(data: bytes) -> ParsedStatement:
    meta, incremental, has_js, encrypted, pages = inspect_structure(data)
    result = ParsedStatement(page_count=pages, needs_ai=False, metadata=meta,
                             incremental_updates=incremental, has_javascript=has_js, encrypted=encrypted)
    if encrypted:
        result.needs_ai, result.reason = True, "encrypted"
        return result

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        row_counter = 0
        for page_no, page in enumerate(pdf.pages, start=1):
            words = _split_merged(page.extract_words(extra_attrs=["fontname", "size"], keep_blank_chars=False))
            if not words:
                result.needs_ai, result.reason = True, "no_text_layer"
                return result
            if page_no == 1:
                _read_header(words, result, page.width, page.height)
            columns = _find_columns(words)
            if columns is None:
                result.needs_ai, result.reason = True, "unknown_layout"
                return result
            header_bottom = columns["_bottom"]
            for line in _group_lines(w for w in words if w["top"] > header_bottom + 2):
                parsed = _parse_line(line, columns, page_no, page.width, page.height, row_counter)
                if parsed is None:
                    if page_no == 1 and result.opening_balance is None and _is_opening(line):
                        amounts = [w for w in line if AMOUNT_RE.match(w["text"])]
                        if amounts:
                            result.opening_balance = parse_amount(amounts[-1]["text"])
                    continue
                result.rows.append(parsed)
                row_counter += 1
    if not result.rows:
        result.needs_ai, result.reason = True, "no_transactions_found"
    return result


def _read_header(words: list[dict], result: ParsedStatement, width: float, height: float) -> None:
    lines = _group_lines(w for w in words if w["top"] < 140)
    header_words = [w for line in lines for w in line]
    if header_words:
        result.header_bbox = norm_box(min(w["x0"] for w in header_words), min(w["top"] for w in header_words),
                                      max(w["x1"] for w in header_words), max(w["bottom"] for w in header_words),
                                      width, height)
    if lines:
        bold = [w["text"] for w in lines[0] if "Bold" in w["fontname"]]
        result.bank_name = " ".join(bold) or None
    text = "\n".join(" ".join(w["text"] for w in line) for line in lines)
    if m := re.search(r"Account holder:\s*(.+?)(?:\s+Period:|$)", text, re.M):
        result.account_holder = m.group(1).strip()
    if m := re.search(r"Account number:\s*X*(\d{4})", text):
        result.account_last4 = m.group(1)
    if m := re.search(r"Period:\s*(\d{2}-\d{2}-\d{4})\s+to\s+(\d{2}-\d{2}-\d{4})", text):
        result.period_from = _to_date(m.group(1))
        result.period_to = _to_date(m.group(2))


def _find_columns(words: list[dict]) -> dict[str, float] | None:
    by_text: dict[str, dict] = {}
    for w in words:
        for key, label in HEADER_KEYS.items():
            if w["text"] == label and key not in by_text and "Bold" in w["fontname"]:
                by_text[key] = w
    if len(by_text) < len(HEADER_KEYS):
        return None
    tops = {round(w["top"]) for w in by_text.values()}
    if max(tops) - min(tops) > 3:
        return None
    return {
        "date_x0": by_text["date"]["x0"], "narr_x0": by_text["narration"]["x0"], "ref_x0": by_text["ref"]["x0"],
        "debit_x1": by_text["debit"]["x1"], "credit_x1": by_text["credit"]["x1"],
        "balance_x1": by_text["balance"]["x1"], "_bottom": max(w["bottom"] for w in by_text.values()),
    }


def _split_merged(words: list[dict]) -> list[dict]:
    """Split tokens where two adjacent columns collided, apportioning x by character width."""
    out: list[dict] = []
    for w in words:
        match = MERGED_RE.match(w["text"])
        if not match:
            out.append(w)
            continue
        head, amount = match.group("head"), match.group("amount")
        per_char = (w["x1"] - w["x0"]) / max(1, len(w["text"]))
        boundary = w["x0"] + per_char * len(head)
        out.append({**w, "text": head, "x1": boundary})
        out.append({**w, "text": amount, "x0": boundary})
    return out


def _group_lines(words) -> list[list[dict]]:  # type: ignore[no-untyped-def]
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"] * 2) / 2, w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _is_opening(line: list[dict]) -> bool:
    return "Opening Balance" in " ".join(w["text"] for w in line)


def _parse_line(line: list[dict], cols: dict[str, float], page_no: int, width: float, height: float,
                row_index: int) -> ParsedRow | None:
    if not DATE_RE.match(line[0]["text"]) or abs(line[0]["x0"] - cols["date_x0"]) > 6:
        return None
    narration = [w["text"] for w in line[1:] if cols["narr_x0"] - 2 <= w["x0"] < cols["ref_x0"] - 2]
    ref = [w["text"] for w in line if cols["ref_x0"] - 2 <= w["x0"] < cols["ref_x0"] + 90
           and not AMOUNT_RE.match(w["text"])]
    cells: dict[str, dict] = {}
    for w in line:
        if not AMOUNT_RE.match(w["text"]):
            continue
        target = min(("debit", "credit", "balance"), key=lambda k: abs(w["x1"] - cols[f"{k}_x1"]))
        if abs(w["x1"] - cols[f"{target}_x1"]) <= 25:
            cells[target] = w
    if "balance" not in cells:
        return None
    amount_cell = cells.get("credit") or cells.get("debit")
    return ParsedRow(
        page=page_no, row_index=row_index, txn_date=_to_date(line[0]["text"]),
        narration=" ".join(narration), reference=" ".join(ref),
        debit=parse_amount(cells["debit"]["text"]) if "debit" in cells else Decimal("0"),
        credit=parse_amount(cells["credit"]["text"]) if "credit" in cells else Decimal("0"),
        balance=parse_amount(cells["balance"]["text"]),
        row_bbox=norm_box(36, min(w["top"] for w in line) - 2, width - 30, max(w["bottom"] for w in line) + 2,
                          width, height),
        amount_bbox=norm_box(amount_cell["x0"] - 2, amount_cell["top"] - 2, amount_cell["x1"] + 2,
                             amount_cell["bottom"] + 2, width, height) if amount_cell else None,
        amount_font=amount_cell["fontname"] if amount_cell else None,
        amount_size=round(float(amount_cell["size"]), 2) if amount_cell else None,
        balance_font=cells["balance"]["fontname"], balance_size=round(float(cells["balance"]["size"]), 2),
    )


def _to_date(text: str) -> date:
    return datetime.strptime(text, "%d-%m-%Y").date()
