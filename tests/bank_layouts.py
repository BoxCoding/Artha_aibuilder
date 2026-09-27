"""Bank-statement PDFs in several real-world layouts, for testing the universal reader.

These imitate the *shape* of common Indian statement formats (column names, date formats, wrapped
narrations, Dr/Cr markers, newest-first order, repeated or single headers). They use made-up banks
and people. They are no substitute for testing against real statements, which the reader must also
be checked against before go-live.
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


@dataclass
class Txn:
    day: date
    narration: str
    ref: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


NARRATIONS_CR = ["UPI/CR/412233/SHARMA TRADERS/Payment for goods supplied", "NEFT CR-SBIN0001234-KAVYA FOODS PVT LTD",
                 "IMPS/P2A/3345/RAHUL ENTERPRISES", "CASH DEPOSIT BY SELF AT BRANCH", "UPI/CR/99812/MEHTA STORES"]
NARRATIONS_DR = ["UPI/DR/771234/AGARWAL WHOLESALE/stock purchase for the month", "NEFT DR-HDFC0000123-SHOP RENT",
                 "ACH D- TP ACH BAJAJ FIN-EMI 0456", "ATM WDL 00456 CONNAUGHT PLACE", "POS 4521XXXX RELIANCE SMART",
                 "ELECTRICITY BILL PAYMENT BESCOM"]


def make_transactions(seed: int = 7, count: int = 70, opening: Decimal = Decimal("84500.00")) -> list[Txn]:
    rng = random.Random(seed)
    day = date(2026, 4, 1)
    balance = opening
    rows: list[Txn] = []
    for i in range(count):
        day += timedelta(days=rng.choice([0, 1, 1, 2, 3]))
        if rng.random() < 0.55 or balance < Decimal("5000"):
            amount = Decimal(rng.randrange(1500, 95000)) + Decimal(rng.randrange(0, 100)) / 100
            balance += amount
            rows.append(Txn(day, rng.choice(NARRATIONS_CR), f"{rng.randrange(10**11, 10**12)}", Decimal("0"), amount,
                            balance))
        else:
            amount = Decimal(rng.randrange(500, min(60000, int(balance) - 1000))) + Decimal(rng.randrange(0, 100)) / 100
            balance -= amount
            rows.append(Txn(day, rng.choice(NARRATIONS_DR), f"{rng.randrange(10**5, 10**6)}", amount, Decimal("0"),
                            balance))
    return rows


def inr(value: Decimal) -> str:
    """Indian digit grouping: 12,34,567.89"""
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.replace("/", "/ ").split():
        candidate = f"{line} {word}".strip() if not line.endswith("/") else f"{line}{word}"
        if len(candidate) > width and line:
            out.append(line)
            line = word
        else:
            line = candidate
    return out + [line] if line else out


LAYOUTS = ("hdfc_like", "sbi_like", "kotak_like", "icici_like")


def render(layout: str, rows: list[Txn], opening: Decimal, holder: str = "PRIYA NAIR",
           account: str = "50100234567891") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setAuthor("Bank statement")
    width, height = A4
    period = (rows[0].day, rows[-1].day)

    def header_block() -> float:
        bank = {"hdfc_like": "Northwind Bank", "sbi_like": "State Bank of India", "kotak_like": "Kotak Mahindra Bank",
                "icici_like": "ICICI Bank"}[layout]
        c.setFont("Helvetica-Bold", 13)
        c.drawString(36, height - 40, bank)
        c.setFont("Helvetica", 8.5)
        c.drawString(36, height - 58, f"Account Name : {holder}")
        c.drawString(36, height - 70, f"Account No : XXXXXXXXXX{account[-4:]}")
        c.drawString(330, height - 58, "Branch : MG ROAD, BENGALURU")
        if layout == "sbi_like":
            c.drawString(36, height - 82, f"Statement From : {period[0]:%d %b %Y} to {period[1]:%d %b %Y}")
        else:
            c.drawString(36, height - 82, f"Statement period From {period[0]:%d/%m/%Y} To {period[1]:%d/%m/%Y}")
        return height - 105

    ordered = list(reversed(rows)) if layout == "kotak_like" else rows
    y = header_block()
    page_header_every_page = layout in ("hdfc_like", "kotak_like", "icici_like")

    def column_header(y: float) -> float:
        c.setFont("Helvetica-Bold", 7.5)
        if layout == "hdfc_like":
            for x, t in [(36, "Date"), (82, "Narration"), (282, "Chq./Ref.No."), (356, "Value Dt")]:
                c.drawString(x, y, t)
            for x1, t in [(460, "Withdrawal Amt."), (515, "Deposit Amt."), (575, "Closing Balance")]:
                c.drawRightString(x1, y, t)
        elif layout == "sbi_like":
            for x, t in [(36, "Txn Date"), (92, "Value Date"), (150, "Description"), (330, "Ref No./Cheque No.")]:
                c.drawString(x, y, t)
            for x1, t in [(470, "Debit"), (520, "Credit"), (575, "Balance")]:
                c.drawRightString(x1, y, t)
        elif layout == "kotak_like":
            for x, t in [(36, "Date"), (90, "Narration"), (300, "Chq/Ref No")]:
                c.drawString(x, y, t)
            c.drawRightString(470, y, "Amount")
            c.drawString(482, y, "Dr/Cr")
            c.drawRightString(575, y, "Balance")
        else:  # icici_like: two-line header
            for x, t in [(36, "Transaction"), (110, "Particulars"), (320, "Cheque")]:
                c.drawString(x, y, t)
            for x1, t in [(460, "Withdrawal"), (515, "Deposit"), (575, "Balance")]:
                c.drawRightString(x1, y, t)
            y -= 9
            c.drawString(36, y, "Date")
            c.drawString(320, y, "Number")
            for x1, t in [(460, "(INR)"), (515, "(INR)"), (575, "(INR)")]:
                c.drawRightString(x1, y, t)
        return y - 14

    y = column_header(y)
    c.setFont("Helvetica", 7.5)
    if layout != "kotak_like":
        c.drawString(82 if layout == "hdfc_like" else 150 if layout == "sbi_like" else 110, y, "Opening Balance")
        c.drawRightString(575, y, inr(opening))
        y -= 13

    for t in ordered:
        narr_x, width_chars = {"hdfc_like": (82, 42), "sbi_like": (150, 38), "kotak_like": (90, 44),
                               "icici_like": (110, 44)}[layout]
        lines = _wrap(t.narration, width_chars)
        if y - 11 * len(lines) < 50:
            c.setFont("Helvetica", 7)
            c.drawCentredString(width / 2, 30, f"Page {c.getPageNumber()}")
            c.showPage()
            y = height - 50
            if page_header_every_page:
                y = column_header(y)
            c.setFont("Helvetica", 7.5)
        if layout == "hdfc_like":
            c.drawString(36, y, f"{t.day:%d/%m/%y}")
            c.drawString(282, y, t.ref[-12:])
            c.drawString(356, y, f"{t.day:%d/%m/%y}")
            if t.debit:
                c.drawRightString(460, y, inr(t.debit))
            if t.credit:
                c.drawRightString(515, y, inr(t.credit))
            c.drawRightString(575, y, inr(t.balance))
        elif layout == "sbi_like":
            c.drawString(36, y, f"{t.day:%-d %b %Y}")
            c.drawString(92, y, f"{t.day:%-d %b %Y}")
            c.drawString(330, y, t.ref[-10:])
            if t.debit:
                c.drawRightString(470, y, inr(t.debit))
            if t.credit:
                c.drawRightString(520, y, inr(t.credit))
            c.drawRightString(575, y, inr(t.balance))
        elif layout == "kotak_like":
            c.drawString(36, y, f"{t.day:%d-%m-%Y}")
            c.drawString(300, y, t.ref[-10:])
            c.drawRightString(470, y, inr(t.debit or t.credit))
            c.drawString(482, y, "DR" if t.debit else "CR")
            c.drawRightString(575, y, f"{inr(t.balance)}(Cr)")
        else:
            c.drawString(36, y, f"{t.day:%d-%b-%Y}")
            c.drawString(320, y, t.ref[-8:])
            if t.debit:
                c.drawRightString(460, y, inr(t.debit))
            if t.credit:
                c.drawRightString(515, y, inr(t.credit))
            c.drawRightString(575, y, inr(t.balance))
        for i, part in enumerate(lines):
            c.drawString(narr_x, y - 9 * i, part)
        y -= 9 * len(lines) + 4
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(82, y - 6, "Closing Balance")
    c.drawRightString(575, y - 6, inr(rows[-1].balance))
    c.showPage()
    c.save()
    return buf.getvalue()


def statement(layout: str, seed: int = 7, count: int = 70) -> tuple[bytes, list[Txn], Decimal]:
    opening = Decimal("84500.00")
    rows = make_transactions(seed, count, opening)
    return render(layout, rows, opening), rows, opening
