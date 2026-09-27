"""Deterministic reader for real-world, text-layer bank statements.

The template reader in `pdf_reader.py` knows exactly one layout. Indian bank statements come in dozens:
different column names ("Withdrawal Amt." / "Debit" / "Dr"), date formats ("01/04/26", "01-Apr-2026",
"01 Apr 2026"), single Amount columns with a Dr/Cr marker, narrations wrapped over several lines,
newest-first ordering, balances suffixed "Cr"/"Dr", headers repeated on every page or only on the first.

This reader finds the transaction table from its header words, assigns every word to a column by
position, and stitches wrapped lines back together. It then *proves* its own reading: the running
balance must reconcile row by row. A statement it cannot reconcile is returned as `needs_ai` rather
than guessed at, because downstream a misread is indistinguishable from a forged balance, and a
forgery finding about a genuine customer is the worst error this system can make.

Output is the same `ParsedStatement` as the template reader, with page coordinates and font metrics
for every amount cell, so every forensic rule and every memo citation works unchanged.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

from app.documents.pdf_reader import ParsedRow, ParsedStatement, inspect_structure, norm_box

# --------------------------------------------------------------------------- vocabulary

COLUMN_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # Order matters: the first match wins, so specific phrases come before generic words.
    ("value_date", ("value date", "value dt", "val date", "value")),
    ("date", ("txn date", "tran date", "trans date", "transaction date", "post date", "posting date", "date")),
    ("balance", ("closing balance", "balance", "bal")),
    ("drcr", ("dr/cr", "cr/dr", "dr / cr", "cr / dr", "type")),
    ("debit", ("withdrawal", "withdrawals", "debit", "debits", "dr amount", "paid out", "dr")),
    ("credit", ("deposit", "deposits", "credit", "credits", "cr amount", "paid in", "cr")),
    ("amount", ("amount", "txn amount", "transaction amount")),
    ("ref", ("chq", "cheque", "ref", "reference", "instrument", "utr")),
    ("narration", ("narration", "description", "particulars", "remarks", "details", "transaction details")),
]
NUMERIC_COLUMNS = {"debit", "credit", "amount", "balance"}

KNOWN_BANKS = (
    "State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank", "Kotak Mahindra Bank", "Punjab National Bank",
    "Bank of Baroda", "Canara Bank", "Union Bank of India", "IDFC FIRST Bank", "IndusInd Bank", "Yes Bank",
    "Federal Bank", "AU Small Finance Bank", "Bank of India", "Indian Bank", "Central Bank of India", "IDBI Bank",
    "RBL Bank", "Bandhan Bank", "South Indian Bank", "Karur Vysya Bank", "Equitas Small Finance Bank",
    "Ujjivan Small Finance Bank", "Indian Overseas Bank", "UCO Bank", "Bank of Maharashtra", "Punjab & Sind Bank",
    "City Union Bank", "Karnataka Bank", "Tamilnad Mercantile Bank", "DCB Bank", "CSB Bank", "Jana Small Finance Bank",
    "Suryoday Small Finance Bank", "ESAF Small Finance Bank", "Airtel Payments Bank", "India Post Payments Bank",
    "Paytm Payments Bank", "Fino Payments Bank", "Kaveri Commercial Bank",
)

# Amounts on Indian statements always carry paise; requiring them keeps cheque and reference
# numbers ("000012345") out of the money columns.
AMOUNT_RE = re.compile(r"^\(?-?(?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2}\)?(?:\s*\(?(?:cr|dr)\)?\.?)?$", re.I)
MARKER_RE = re.compile(r"^\(?(cr|dr|credit|debit|c|d)\)?\.?$", re.I)
SKIP_LINE_RE = re.compile(r"(closing balance|total|grand total|statement summary|\bpage \d+( of \d+)?\b|"
                          r"carried forward|brought forward|c/f|b/f|end of statement|\*\*\*|computer generated|"
                          r"does not require|signature|\bdr count\b|\bcr count\b|synthetic test statement|"
                          r"please (examine|verify)|contents of this statement|registered office)", re.I)
OPENING_RE = re.compile(r"(opening balance|balance b/f|brought forward|balance forward|b/f)", re.I)

DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y", "%d-%b-%Y", "%d-%b-%y",
                "%d %b %Y", "%d %b %y", "%d/%b/%Y", "%d/%b/%y", "%d %B %Y", "%d-%B-%Y", "%Y-%m-%d", "%d%b%Y")
DATE_TOKEN_RE = re.compile(r"^\d{1,2}[-/. ]?(?:\d{1,2}|[A-Za-z]{3,9})[-/. ]?\d{2,4}$|^\d{4}-\d{2}-\d{2}$")


def parse_date(text: str) -> date | None:
    text = text.strip().replace(",", " ").replace("  ", " ")
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if 2000 <= parsed.year <= 2100:
            return parsed
    return None


def parse_money(text: str) -> tuple[Decimal, str | None] | None:
    """'1,23,456.78' -> (123456.78, None); '5,000.00Cr' -> (5000, 'cr'); '(250.00)' -> (-250, None)."""
    raw = text.strip()
    if not AMOUNT_RE.match(raw):
        return None
    marker = None
    if m := re.search(r"\(?(cr|dr)\)?\.?$", raw, re.I):
        marker = m.group(1).lower()
        raw = raw[: m.start()].strip()
    negative = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
    raw = raw.strip("()").lstrip("-").replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return (-value if negative else value), marker


# --------------------------------------------------------------------------- layout

@dataclass
class Column:
    kind: str
    x0: float
    x1: float

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Table:
    columns: list[Column]
    header_bottom: float

    def kinds(self) -> set[str]:
        return {c.kind for c in self.columns}

    def column_for(self, word: dict) -> Column | None:
        """Money goes only to money columns, matched on the right edge (amounts are right-aligned).
        Everything else goes to the text column whose span holds the word's left edge."""
        if AMOUNT_RE.match(word["text"]):
            numeric = [c for c in self.columns if c.kind in NUMERIC_COLUMNS]
            if numeric:
                best = min(numeric, key=lambda c: abs(word["x1"] - c.x1))
                if abs(word["x1"] - best.x1) <= 45:
                    return best
        text_cols = [c for c in self.columns if c.kind not in NUMERIC_COLUMNS]
        inside = [c for c in text_cols if c.x0 - 4 <= word["x0"] <= c.x1 + 4]
        if inside:
            return min(inside, key=lambda c: abs(word["x0"] - c.x0))
        if MARKER_RE.match(word["text"]):
            return next((c for c in self.columns if c.kind == "drcr"), None)
        return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z/ ]", "", text.lower().replace(".", " ").replace("(", " ").replace(")", " ")).strip()


def _classify(phrase: str, exact: bool = False) -> str | None:
    phrase = " ".join(_norm(phrase).replace("/", " / ").split()).replace(" / ", "/")
    if not phrase:
        return None
    for kind, keys in COLUMN_KEYWORDS:
        for key in keys:
            if phrase == key:
                return kind
    if exact:
        return None
    first = re.split(r"[ /]", phrase)[0]
    for kind, keys in COLUMN_KEYWORDS:
        if first in keys:
            return kind
    return None


def group_lines(words: list[dict], tolerance: float = 2.5) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tolerance:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def find_table(lines: list[list[dict]]) -> Table | None:
    """Locate the header row: a line (optionally with a wrapped second line) that names a date, a
    balance and either debit+credit or a single amount column."""
    for i, line in enumerate(lines[:60]):
        candidates = [line]
        if i + 1 < len(lines) and 0 < lines[i + 1][0]["top"] - line[0]["top"] <= 14:
            candidates.append(line + lines[i + 1])
        for words in candidates:
            columns = _columns_from_header(words)
            if columns is None:
                continue
            kinds = {c.kind for c in columns}
            has_money = {"debit", "credit"} <= kinds or "amount" in kinds
            if "date" in kinds and "balance" in kinds and has_money and kinds & {"narration", "ref"}:
                return Table(columns=columns, header_bottom=max(w["bottom"] for w in words))
    return None


@dataclass
class _Segment:
    words: list[dict]
    kind: str | None

    @property
    def x0(self) -> float:
        return min(w["x0"] for w in self.words)

    @property
    def x1(self) -> float:
        return max(w["x1"] for w in self.words)

    @property
    def phrase(self) -> str:
        return " ".join(w["text"] for w in sorted(self.words, key=lambda w: (round(w["top"]), w["x0"])))


def _segments(line: list[dict]) -> list[_Segment]:
    """Greedy left-to-right phrase matching: the longest known column name wins ("Closing Balance",
    "Value Dt", "Txn Date"), then one-word names ("Deposit"); qualifiers such as "Amt." or "(INR)"
    attach to the column on their left."""
    segments: list[_Segment] = []
    i = 0
    while i < len(line):
        matched = False
        for n in (3, 2):
            chunk = line[i:i + n]
            if len(chunk) == n and all(b["x0"] - a["x1"] <= 12 for a, b in zip(chunk, chunk[1:])):
                kind = _classify(" ".join(w["text"] for w in chunk), exact=True)
                if kind:
                    segments.append(_Segment(list(chunk), kind))
                    i += n
                    matched = True
                    break
        if matched:
            continue
        word = line[i]
        kind = _classify(word["text"])
        if kind is None and segments and word["x0"] - segments[-1].x1 <= 12:
            segments[-1].words.append(word)  # "Amt.", "(INR)", "No." and similar qualifiers
        else:
            segments.append(_Segment([word], kind))
        i += 1
    return segments


def _columns_from_header(words: list[dict]) -> list[Column] | None:
    by_line = group_lines(words)
    segments = _segments(by_line[0])
    for extra in by_line[1:]:
        # A wrapped second header line ("Date" under "Transaction", "(INR)" under "Deposit").
        for w in extra:
            host = next((s for s in segments if s.x0 - 6 <= w["x0"] <= s.x1 + 6 or s.x0 - 6 <= w["x1"] <= s.x1 + 6),
                        None)
            if host is not None:
                host.words.append(w)
    columns: list[Column] = []
    for seg in segments:
        kind = _classify(seg.phrase, exact=True) or seg.kind or _classify(seg.phrase)
        if kind is None:
            continue
        if kind == "date" and any(c.kind == "date" for c in columns):
            kind = "value_date"
        if any(c.kind == kind for c in columns):
            continue
        columns.append(Column(kind, seg.x0, seg.x1))
    if len(columns) < 4:
        return None
    columns.sort(key=lambda c: c.x0)
    # Stretch text columns to the next column's start, so long narrations land where they belong.
    for left, right in zip(columns, columns[1:]):
        if left.kind not in NUMERIC_COLUMNS:
            left.x1 = max(left.x1, right.x0 - 4)
    return columns


# --------------------------------------------------------------------------- rows

@dataclass
class _Row:
    page: int
    words: list[dict]
    cells: dict[str, list[dict]]


def _cells(line: list[dict], table: Table) -> dict[str, list[dict]]:
    cells: dict[str, list[dict]] = {}
    for w in line:
        col = table.column_for(w)
        if col is not None:
            cells.setdefault(col.kind, []).append(w)
    return cells


def _row_date(cells: dict[str, list[dict]]) -> date | None:
    words = cells.get("date", [])
    if not words:
        return None
    joined = " ".join(w["text"] for w in words[:3])
    for candidate in (joined, words[0]["text"], " ".join(w["text"] for w in words[:2])):
        parsed = parse_date(candidate)
        if parsed:
            return parsed
    return None


def _money_cell(words: list[dict]) -> tuple[Decimal, str | None, dict] | None:
    """The amount in a cell, with a trailing Cr/Dr marker that may be a separate word."""
    numeric = [w for w in words if parse_money(w["text"])]
    if not numeric:
        return None
    main = numeric[-1]
    value, marker = parse_money(main["text"])  # type: ignore[misc]
    if marker is None:
        after = [w for w in words if w["x0"] >= main["x1"] - 1 and MARKER_RE.match(w["text"])]
        if after:
            marker = "cr" if after[0]["text"].strip("().").lower().startswith("c") else "dr"
    return value, marker, main


def read_universal(data: bytes, password: str | None = None) -> ParsedStatement:
    meta, incremental, has_js, encrypted, pages = inspect_structure(data)
    result = ParsedStatement(page_count=pages, needs_ai=False, metadata=meta, incremental_updates=incremental,
                             has_javascript=has_js, encrypted=encrypted)
    if encrypted and not password:
        result.needs_ai, result.reason = True, "encrypted"
        return result
    try:
        pdf = pdfplumber.open(io.BytesIO(data), password=password or "")
    except Exception:  # noqa: BLE001 - wrong password, or a file pdfminer cannot read
        result.needs_ai, result.reason = True, "unreadable"
        return result

    raw_rows: list[_Row] = []
    table: Table | None = None
    page_sizes: dict[int, tuple[float, float]] = {}
    header_text: list[str] = []
    with pdf:
        result.page_count = result.page_count or len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            page_sizes[page_no] = (float(page.width), float(page.height))
            words = page.extract_words(extra_attrs=["fontname", "size"], keep_blank_chars=False, use_text_flow=False)
            if not words:
                if page_no == 1:
                    result.needs_ai, result.reason = True, "no_text_layer"
                    return result
                continue
            lines = group_lines(words)
            page_table = find_table(lines)
            if page_table is not None:
                table = page_table
                start = page_table.header_bottom + 1
            elif table is not None:
                start = -1  # header only on the first page; reuse its columns
            else:
                header_text.extend(" ".join(w["text"] for w in line) for line in lines)
                continue
            if page_no == 1 or not header_text:
                header_text.extend(" ".join(w["text"] for w in line) for line in lines if line[0]["top"] < start)
                if page_no == 1 and lines:
                    top_words = [w for line in lines if line[0]["top"] < max(start, 0) for w in line]
                    if top_words:
                        result.header_bbox = norm_box(min(w["x0"] for w in top_words), min(w["top"] for w in top_words),
                                                      max(w["x1"] for w in top_words),
                                                      max(w["bottom"] for w in top_words), *page_sizes[page_no])
            for line in lines:
                if line[0]["top"] <= start:
                    continue
                text = " ".join(w["text"] for w in line)
                cells = _cells(line, table)
                if OPENING_RE.search(text) and result.opening_balance is None and not raw_rows:
                    money = _money_cell(cells.get("balance", []) or line)
                    if money:
                        result.opening_balance = -money[0] if money[1] == "dr" else money[0]
                    continue
                if SKIP_LINE_RE.search(text) and _row_date(cells) is None:
                    continue
                if _row_date(cells) is not None and _money_cell(cells.get("balance", [])):
                    raw_rows.append(_Row(page_no, list(line), cells))
                elif raw_rows and raw_rows[-1].page == page_no and not any(
                        _money_cell(cells.get(k, [])) for k in NUMERIC_COLUMNS):
                    # A wrapped narration (or reference) line belongs to the transaction above it.
                    for kind in ("narration", "ref"):
                        if cells.get(kind):
                            raw_rows[-1].cells.setdefault(kind, []).extend(cells[kind])
                    raw_rows[-1].words.extend(line)

    if table is None:
        result.needs_ai, result.reason = True, "unknown_layout"
        return result
    _read_header_fields(result, "\n".join(header_text), meta)
    rows = _build_rows(raw_rows, table, page_sizes)
    if not rows:
        result.needs_ai, result.reason = True, "no_transactions_found"
        return result
    rows = _chronological(rows)
    if result.opening_balance is None:
        first = rows[0]
        result.opening_balance = first.balance - first.credit + first.debit
    if not _reconciles(rows, result.opening_balance):
        result.needs_ai, result.reason = True, "balances_do_not_reconcile"
        result.rows = rows
        return result
    for index, row in enumerate(rows):
        row.row_index = index
    result.rows = rows
    if result.period_from is None:
        result.period_from, result.period_to = rows[0].txn_date, rows[-1].txn_date
    return result


def _build_rows(raw_rows: list[_Row], table: Table, page_sizes: dict[int, tuple[float, float]]) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    kinds = table.kinds()
    previous_balance: Decimal | None = None
    for raw in raw_rows:
        cells = raw.cells
        txn_date = _row_date(cells)
        balance_cell = _money_cell(cells.get("balance", []))
        if txn_date is None or balance_cell is None:
            continue
        balance = -balance_cell[0] if balance_cell[1] == "dr" else balance_cell[0]
        debit = credit = Decimal("0")
        amount_word: dict | None = None
        if {"debit", "credit"} <= kinds:
            d = _money_cell(cells.get("debit", []))
            c = _money_cell(cells.get("credit", []))
            if d:
                debit, amount_word = abs(d[0]), d[2]
            if c:
                credit, amount_word = abs(c[0]), c[2]
        else:
            a = _money_cell(cells.get("amount", []))
            if a:
                amount_word = a[2]
                marker = a[1]
                if marker is None and cells.get("drcr"):
                    token = cells["drcr"][0]["text"].lower()
                    marker = "cr" if token.startswith("c") else "dr" if token.startswith("d") else None
                if marker is None and previous_balance is not None:
                    marker = "cr" if balance >= previous_balance else "dr"
                if a[0] < 0:
                    marker = "dr"
                if marker == "cr":
                    credit = abs(a[0])
                else:
                    debit = abs(a[0])
        previous_balance = balance
        width, height = page_sizes[raw.page]
        words = raw.words
        narration = " ".join(w["text"] for w in sorted(cells.get("narration", []), key=lambda w: (w["top"], w["x0"])))
        reference = " ".join(w["text"] for w in cells.get("ref", []))
        balance_word = balance_cell[2]
        rows.append(ParsedRow(
            page=raw.page, row_index=len(rows), txn_date=txn_date, narration=narration[:240],
            reference=reference[:60], debit=debit, credit=credit, balance=balance,
            row_bbox=norm_box(min(w["x0"] for w in words) - 2, min(w["top"] for w in words) - 2,
                              max(w["x1"] for w in words) + 2, max(w["bottom"] for w in words) + 2, width, height),
            amount_bbox=norm_box(amount_word["x0"] - 2, amount_word["top"] - 2, amount_word["x1"] + 2,
                                 amount_word["bottom"] + 2, width, height) if amount_word else None,
            amount_font=amount_word["fontname"] if amount_word else None,
            amount_size=round(float(amount_word["size"]), 2) if amount_word else None,
            balance_font=balance_word["fontname"], balance_size=round(float(balance_word["size"]), 2),
        ))
    return rows


def _chronological(rows: list[ParsedRow]) -> list[ParsedRow]:
    """Some banks print newest first. Reverse when dates clearly run backwards."""
    if len(rows) >= 2 and rows[0].txn_date > rows[-1].txn_date:
        return list(reversed(rows))
    return rows


def reconciles(rows: list[ParsedRow], opening: Decimal | None) -> bool:
    if not rows:
        return False
    if opening is None:
        opening = rows[0].balance - rows[0].credit + rows[0].debit
    return _reconciles(rows, opening)


def _reconciles(rows: list[ParsedRow], opening: Decimal) -> bool:
    """The reading is trusted only if the running balance holds almost everywhere. One or two breaks
    are exactly what forensic rules exist to find; many breaks mean we misread the layout."""
    breaks = 0
    previous = opening
    for row in rows:
        if abs(previous + row.credit - row.debit - row.balance) > Decimal("0.01"):
            breaks += 1
        previous = row.balance
    return breaks <= max(2, len(rows) * 3 // 100)


def _read_header_fields(result: ParsedStatement, text: str, meta: dict[str, str]) -> None:
    flat = " ".join(text.split())
    lowered = flat.lower()
    for bank in KNOWN_BANKS:
        if bank.lower() in lowered:
            result.bank_name = bank
            break
    if result.bank_name is None:
        author = (meta.get("Author") or "").strip()
        first = text.strip().splitlines()[0].strip() if text.strip() else ""
        # Banks often set the PDF author to their own name; prefer it when it plausibly is one.
        result.bank_name = (author if "bank" in author.lower() else first or author)[:60] or None

    name_patterns = (r"(?:account holder|account name|a/c name|customer name|name of (?:the )?customer|name)\s*[:\-]?\s*"
                     r"([A-Z][A-Za-z.&' ]{2,60}?)(?=\s{2,}|\s+(?:account|a/c|address|cust\w*|period|statement|branch|ifsc|from)\b|$)")
    if m := re.search(name_patterns, flat, re.I):
        result.account_holder = m.group(1).strip(" .:-")
    if m := re.search(r"(?:account|a/c)\s*(?:no|number|#)\.?\s*[:\-]?\s*[X*x\d\- ]*?(\d{4})\b", flat, re.I):
        result.account_last4 = m.group(1)
    date_pat = r"(\d{1,2}[-/. ](?:\d{1,2}|[A-Za-z]{3,9})[-/. ]\d{2,4})"
    if m := re.search(rf"(?:from|period)\s*[:\-]?\s*{date_pat}\s*(?:to|till|until|-)\s*{date_pat}", flat, re.I):
        result.period_from, result.period_to = parse_date(m.group(1)), parse_date(m.group(2))
