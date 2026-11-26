"""An ICICI-style savings statement for tests: a seven-column grid with two-line headers and transaction
remarks that wrap over two to three lines, centred against the row's date and amounts. Any page count."""
from __future__ import annotations

import io
import random
from datetime import date, timedelta
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

COLS = [("S No.", 30, 58), ("Transaction|Date", 60, 120), ("Cheque Number", 122, 190),
        ("Transaction Remarks", 192, 400), ("Withdrawal|Amount (INR)", 402, 465),
        ("Deposit|Amount (INR)", 467, 520), ("Balance|(INR)", 522, 575)]
REMARKS = ["UPI/FOOD ORDER LTD/order@testbank/Food Pay/TEST BANK/000000000001/TESTREF000000000001",
           "MMT/IMPS/000000000002/Fund transfer from Sample Finance/000000AB001/to TEST CUSTOMER",
           "NEFT-TEST0000000000000003-SAMPLE DIVIDEND-/ATTN //-0000000000004",
           "UPI/SAMPLE STORE/samplestore@testupi/payment/TEST BANK/000000000005",
           "UPI/SAMPLE LOANS/sampleloans/UPIintent/TEST BANK/000000000006",
           "ATM/CASH WDL/SAMPLE CITY/0007"]


def _wrap(text: str, width: int = 44) -> list[str]:
    return [text[i:i + width] for i in range(0, len(text), width)][:3]


def build(pages: int, rows_per_page: int = 11, seed: int = 7) -> bytes:
    rnd = random.Random(seed)
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    balance = Decimal("25000.00")
    day = date(2025, 10, 1)
    n = 0
    for page in range(1, pages + 1):
        y = height - 60
        if page == 1:
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(130, y, "Statement of Transactions in Saving Account no. 000000000001 in INR for the period")
            pdf.setFont("Helvetica", 8)
            for i, line in enumerate(["TEST CUSTOMER", "1 SAMPLE STREET", "SAMPLE CITY - INDIA - 000000"]):
                pdf.drawString(30, y - 24 - i * 10, line)
            y -= 70
        pdf.setFont("Helvetica-Bold", 7)
        for name, x0, x1 in COLS:
            for i, part in enumerate(name.split("|")):
                pdf.drawString(x0 + 2, y - i * 8, part)
        y -= 26
        pdf.setFont("Helvetica", 7)
        for _ in range(rows_per_page):
            n += 1
            if rnd.random() < 0.35:
                day += timedelta(days=1)
            debit = Decimal(rnd.randint(50, 9000)) + Decimal(rnd.randint(0, 99)) / 100 if rnd.random() < 0.6 else None
            credit = None if debit else Decimal(rnd.randint(500, 25000)) + Decimal(rnd.randint(0, 99)) / 100
            balance += (credit or 0) - (debit or 0)
            lines = _wrap(rnd.choice(REMARKS))
            mid = y - (len(lines) - 1) * 4.5  # the date and amounts sit in the middle of the wrapped remarks
            for i, line in enumerate(lines):
                pdf.drawString(194, y - i * 9, line)
            pdf.drawString(32, mid, str(n))
            pdf.drawString(62, mid, day.strftime("%d.%m.%Y"))
            if debit:
                pdf.drawRightString(463, mid, f"{debit:.2f}")
            if credit:
                pdf.drawRightString(518, mid, f"{credit:.2f}")
            pdf.drawRightString(573, mid, f"{balance:.2f}")
            y -= 9 * len(lines) + 8
        pdf.drawString(30, 30, f"Page {page} of {pages}")
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


def build_ruled(pages: int, rows_per_page: int = 12, seed: int = 11) -> bytes:
    """The net-banking download as ICICI actually prints it: a ruled grid, a header wrapped over three lines
    with the remarks title centred far right of where remarks start, the first remark line half a line
    above the date, and a contact footer under every page. Fictional data."""
    rnd = random.Random(seed)
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    rules = [19, 49, 119, 189, 390, 456, 522, 574]
    balance, day, n = Decimal("25000.00"), date(2025, 10, 1), 0
    for page in range(1, pages + 1):
        y = height - 84
        pdf.setFont("Helvetica-Bold", 7)
        for text, x, row in [("Transaction", 60, 0), ("Withdrawal", 399, 0), ("Deposit", 474, 0), ("Balance", 532, 0),
                             ("S No.", 24, 1), ("Cheque Number", 122, 1), ("Transaction Remarks", 247, 1),
                             ("Date", 74, 2), ("Amount (INR)", 396, 2), ("Amount (INR)", 462, 2), ("(INR)", 538, 2)]:
            pdf.drawString(x, y - row * 5, text)
        top = y + 8
        y -= 20
        pdf.line(rules[0], y + 7, rules[-1], y + 7)
        pdf.setFont("Helvetica", 7)
        for _ in range(rows_per_page):
            n += 1
            day += timedelta(days=rnd.random() < 0.4)
            debit = Decimal(rnd.randint(50, 9000)) + Decimal(rnd.randint(0, 99)) / 100 if rnd.random() < 0.6 else None
            credit = None if debit else Decimal(rnd.randint(500, 25000)) + Decimal(rnd.randint(0, 99)) / 100
            balance += (credit or 0) - (debit or 0)
            lines = _wrap(rnd.choice(REMARKS))
            for i, line in enumerate(lines):
                pdf.drawString(192, y - i * 10, line)
            mid = y - 5  # half a line below the first remark line
            pdf.drawString(28, mid, str(n))
            pdf.drawString(61, mid, day.strftime("%d.%m.%Y"))
            if debit:
                pdf.drawRightString(453, mid, f"{debit:.2f}")
            if credit:
                pdf.drawRightString(518, mid, f"{credit:.2f}")
            pdf.drawRightString(571, mid, f"{balance:.2f}")
            y -= 10 * len(lines) + 10
            pdf.line(rules[0], y + 7, rules[-1], y + 7)
        for x in rules:
            pdf.line(x, top, x, y + 7)
        pdf.drawString(30, 40, "www.testbank.example Dial your Bank 0000-0000")
        pdf.drawString(30, 30, "Never share your OTP, CVV or passwords with anyone.")
        pdf.showPage()
    pdf.save()
    return buf.getvalue()
