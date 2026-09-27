"""Realistic synthetic bank statements for testing LedgerLens end to end.

Each statement imitates the *structure* of a real Indian current-account statement: a bank header
with branch, IFSC and customer ID, a transaction table in one of four common layouts, six months of
plausible business activity (UPI receipts with VPAs, NEFT/IMPS with UTR numbers, card settlements,
cash deposits, supplier payments, rent, salaries, GST, electricity, NACH EMIs and returns), a summary
block, and page footers.

Banks and people are fictional, and every page footer says the document is a synthetic test
statement: these files are for testing a lender's own system, never to pass off as a bank's document.

    python -m app.cli make-statements --out var/samples
"""
from __future__ import annotations

import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

TWO = Decimal("0.01")


@dataclass
class Row:
    day: date
    narration: str
    ref: str
    debit: Decimal
    credit: Decimal
    balance: Decimal = Decimal("0")
    font: str = "Helvetica"


@dataclass
class Profile:
    key: str
    holder: str
    business: str
    pan: str
    mobile: str
    industry: str
    bank: str
    branch: str
    ifsc: str
    layout: str
    account: str
    scale: float                       # typical daily receipts in rupees
    cash_share: float = 0.1
    emi: int = 0                       # existing loan instalment
    bounces: int = 0                   # returned EMIs in the last three months
    counterparty: str | None = None    # a named payer injected into the narrations
    big_cash_month: bool = False       # a month with ₹10 lakh+ cash deposits
    notes: list[str] = field(default_factory=list)


PAYERS = ["RAMESH KUMAR", "ANITA SHARMA", "MOHAN STORES", "S V ENTERPRISES", "LAKSHMI CATERERS", "DEEPAK JAIN",
          "KAVERI FOODS PVT LTD", "SUNIL TRADERS", "ASHA MEDICALS", "GREEN LEAF CAFE", "ROHIT VERMA", "PRAKASH AGENCIES"]
PSP = ["okaxis", "oksbi", "okhdfcbank", "ybl", "paytm", "ibl"]
SUPPLIERS = ["AGARWAL WHOLESALE", "METRO CASH N CARRY", "SRI BALAJI DISTRIBUTORS", "NATIONAL PACKAGING",
             "FRESH FARM SUPPLIES", "SHREE GANESH TRADING"]


def _vpa(name: str, rng: random.Random) -> str:
    return f"{name.split()[0].lower()}{rng.randint(10, 99)}@{rng.choice(PSP)}"


def _utr(rng: random.Random, prefix: str = "N") -> str:
    return f"{prefix}{rng.randint(10**14, 10**15 - 1)}"


def _amount(rng: random.Random, base: float, spread: float = 0.45) -> Decimal:
    return Decimal(str(max(50.0, rng.gauss(base, base * spread)))).quantize(TWO)


def build_rows(p: Profile, start: date, months: int, seed: int) -> tuple[Decimal, list[Row]]:
    rng = random.Random(seed)
    opening = Decimal(str(round(p.scale * 12, -2))).quantize(TWO)
    events: list[Row] = []
    end = date(start.year + (start.month - 1 + months) // 12, (start.month - 1 + months) % 12 + 1, 1) - timedelta(days=1)
    day = start
    while day <= end:
        weekday = day.weekday()
        busy = 0.55 if weekday == 6 else 1.0
        for _ in range(rng.choice([1, 2, 2, 3, 3, 4]) if rng.random() < 0.85 * busy else 0):
            payer = rng.choice(PAYERS)
            kind = rng.random()
            if kind < 0.72 - p.cash_share / 2:
                events.append(Row(day, f"UPI/CR/{rng.randint(10**11, 10**12 - 1)}/{payer}/{_vpa(payer, rng)}/UPI",
                                  f"{rng.randint(10**11, 10**12 - 1)}", Decimal("0"), _amount(rng, p.scale / 2.4)))
            elif kind < 0.86 - p.cash_share / 2:
                events.append(Row(day, f"NEFT CR-{rng.choice(['HDFC', 'ICIC', 'SBIN', 'UTIB'])}0{rng.randint(100000, 999999)}-{payer}-{_utr(rng)}",
                                  _utr(rng), Decimal("0"), _amount(rng, p.scale * 1.8)))
            elif kind < 0.92:
                events.append(Row(day, f"POS SETTLEMENT MID {rng.randint(10**7, 10**8 - 1)} {day:%d%m}",
                                  f"{rng.randint(10**9, 10**10 - 1)}", Decimal("0"), _amount(rng, p.scale)))
            else:
                events.append(Row(day, f"CASH DEP {p.branch.split(',')[0].upper()} BRANCH", f"{rng.randint(100000, 999999)}",
                                  Decimal("0"), Decimal(str(round(max(500, rng.gauss(p.scale * 2.5, p.scale)), -2))).quantize(TWO)))
        if weekday in (1, 4) and rng.random() < 0.8:
            supplier = rng.choice(SUPPLIERS)
            events.append(Row(day, f"UPI/DR/{rng.randint(10**11, 10**12 - 1)}/{supplier}/{_vpa(supplier, rng)}/Purchase",
                              f"{rng.randint(10**11, 10**12 - 1)}", _amount(rng, p.scale * 1.9, 0.3), Decimal("0")))
        if day.day == 3:
            events.append(Row(day, "NEFT DR-HDFC0001234-SHOP RENT M K LANDLORD", _utr(rng), Decimal(str(round(p.scale * 2.2, -2))).quantize(TWO), Decimal("0")))
        if day.day == 5 and p.emi:
            lender = "BAJAJ FINANCE"
            events.append(Row(day, f"NACH DR {lender} EMI LN{seed}{day:%m}", f"{rng.randint(10**9, 10**10 - 1)}", Decimal(p.emi).quantize(TWO), Decimal("0")))
        if day.day == 10:
            events.append(Row(day, f"BILLPAY/ELECTRICITY BOARD/CA{rng.randint(10**8, 10**9 - 1)}", f"{rng.randint(10**9, 10**10 - 1)}",
                              _amount(rng, p.scale * 0.35, 0.2), Decimal("0")))
        if day.day == 20:
            events.append(Row(day, f"GST PAYMENT CPIN {rng.randint(10**13, 10**14 - 1)}", f"{rng.randint(10**9, 10**10 - 1)}",
                              _amount(rng, p.scale * 0.6, 0.25), Decimal("0")))
        if day.day == 28:
            events.append(Row(day, "NEFT DR-SBIN0004567-SALARY STAFF WAGES", _utr(rng), Decimal(str(round(p.scale * 3, -2))).quantize(TWO), Decimal("0")))
        if weekday == 5 and rng.random() < 0.35:
            events.append(Row(day, f"ATM WDL {rng.randint(1000, 9999)} {p.branch.split(',')[0].upper()}", f"{rng.randint(100000, 999999)}",
                              Decimal(rng.choice([2000, 3000, 5000, 10000])).quantize(TWO), Decimal("0")))
        if day.day == 1 and day.month % 3 == 1:
            events.append(Row(day, "SMS ALERT CHARGES QTR", "", Decimal("17.70"), Decimal("0")))
        day += timedelta(days=1)

    # Scenario injections
    if p.counterparty:
        for offset in (18, 49, 101):
            d = start + timedelta(days=offset)
            events.append(Row(d, f"NEFT CR-KKBK0{rng.randint(100000, 999999)}-{p.counterparty}-{_utr(rng)}", _utr(rng),
                              Decimal("0"), Decimal(str(round(p.scale * 9, -3))).quantize(TWO)))
    if p.big_cash_month:
        month_start = start + timedelta(days=95)
        for i in range(6):
            events.append(Row(month_start + timedelta(days=i * 4), f"CASH DEP {p.branch.split(',')[0].upper()} BRANCH",
                              f"{rng.randint(100000, 999999)}", Decimal("0"), Decimal("190000.00")))
    if p.bounces and p.emi:
        emi_days = sorted({r.day for r in events if r.narration.startswith("NACH DR")})[-p.bounces:]
        for d in emi_days:
            events.append(Row(d, f"NACH RTN INSUFFICIENT FUNDS BAJAJ FINANCE", "", Decimal("0"), Decimal(p.emi).quantize(TWO)))
            events.append(Row(d, "NACH RTN CHG INCL GST", "", Decimal("590.00"), Decimal("0")))

    events.sort(key=lambda r: (r.day, r.debit > 0))
    balance = opening
    rows: list[Row] = []
    for r in events:
        if r.debit and balance - r.debit < Decimal("2000") and not r.narration.startswith("NACH"):
            continue  # a real business would not have let this payment through
        balance = balance + r.credit - r.debit
        r.balance = balance
        rows.append(r)
    return opening, rows


# --------------------------------------------------------------------------- rendering

def inr(v: Decimal) -> str:
    sign = "-" if v < 0 else ""
    whole, frac = f"{abs(v):.2f}".split(".")
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
    for chunk in text.replace("/", "/ ").split():
        candidate = f"{line}{chunk}" if line.endswith("/") else f"{line} {chunk}".strip()
        if len(candidate) > width and line:
            out.append(line)
            line = chunk
        else:
            line = candidate
    return out + ([line] if line else [])


LAYOUTS = {
    "hdfc_like": {"cols": [(36, "Date", "l"), (80, "Narration", "l"), (290, "Chq./Ref.No.", "l"), (362, "Value Dt", "l"),
                           (462, "Withdrawal Amt.", "r"), (516, "Deposit Amt.", "r"), (575, "Closing Balance", "r")],
                  "date": "%d/%m/%y", "narr_x": 80, "wrap": 44},
    "sbi_like": {"cols": [(36, "Txn Date", "l"), (94, "Value Date", "l"), (152, "Description", "l"),
                          (338, "Ref No./Cheque No.", "l"), (472, "Debit", "r"), (522, "Credit", "r"), (575, "Balance", "r")],
                 "date": "%-d %b %Y", "narr_x": 152, "wrap": 38},
    "icici_like": {"cols": [(36, "Transaction Date", "l"), (112, "Particulars", "l"), (322, "Cheque Number", "l"),
                            (462, "Withdrawal (INR)", "r"), (516, "Deposit (INR)", "r"), (575, "Balance (INR)", "r")],
                   "date": "%d-%b-%Y", "narr_x": 112, "wrap": 44},
    "kotak_like": {"cols": [(36, "Date", "l"), (90, "Narration", "l"), (302, "Chq/Ref No", "l"), (470, "Amount", "r"),
                            (482, "Dr/Cr", "l"), (575, "Balance", "r")],
                   "date": "%d-%m-%Y", "narr_x": 90, "wrap": 44, "newest_first": True},
}


def render(p: Profile, opening: Decimal, rows: list[Row], *, producer: str = "CoreStatement Engine 7.2",
           mod_date_later: bool = False) -> bytes:
    spec = LAYOUTS[p.layout]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=1)
    c.setAuthor(p.bank)
    c.setTitle(f"Account Statement {p.account[-4:]}")
    c.setProducer(producer)
    c.setCreator(producer)
    width, height = A4
    period = (rows[0].day, rows[-1].day)
    ordered = list(reversed(rows)) if spec.get("newest_first") else rows
    total_pages = [0]

    def footer() -> None:
        c.setFont("Helvetica", 6.5)
        c.setFillGray(0.4)
        c.drawString(36, 28, "This is a computer generated statement and does not require a signature.")
        c.drawString(36, 20, f"SYNTHETIC TEST STATEMENT - fictional bank and customer, generated for testing only.")
        c.drawRightString(width - 36, 24, f"Page {c.getPageNumber()}")
        c.setFillGray(0)

    def header_block() -> float:
        c.setFillColorRGB(0.08, 0.2, 0.42)
        c.rect(36, height - 58, 22, 22, fill=1, stroke=0)
        c.setFillGray(1)
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(47, height - 52, p.bank[0])
        c.setFillGray(0)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(64, height - 52, p.bank)
        c.setFont("Helvetica", 7.5)
        c.drawRightString(width - 36, height - 44, f"{p.branch} Branch")
        c.drawRightString(width - 36, height - 54, f"IFSC: {p.ifsc}   MICR: {p.ifsc[-6:]}002")
        c.setStrokeGray(0.6)
        c.line(36, height - 66, width - 36, height - 66)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(36, height - 82, "STATEMENT OF ACCOUNT")
        c.setFont("Helvetica", 8)
        lines = [f"Account Name : {p.holder}", f"Customer ID : {p.account[:6]}",
                 f"Account No : XXXXXXXX{p.account[-4:]}", f"Account Type : Current Account - {p.business}",
                 f"Statement Period : From {period[0]:%d/%m/%Y} To {period[1]:%d/%m/%Y}", "Currency : INR"]
        for i, line in enumerate(lines):
            c.drawString(36 if i % 2 == 0 else 320, height - 98 - (i // 2) * 12, line)
        return height - 150

    def column_header(y: float) -> float:
        c.setFillColorRGB(0.92, 0.94, 0.97)
        c.rect(32, y - 4, width - 64, 14, fill=1, stroke=0)
        c.setFillGray(0)
        c.setFont("Helvetica-Bold", 7.3)
        for x, title, align in spec["cols"]:
            (c.drawRightString if align == "r" else c.drawString)(x, y, title)
        return y - 16

    y = column_header(header_block())
    c.setFont("Helvetica", 7.3)
    if not spec.get("newest_first"):
        c.drawString(spec["narr_x"], y, "Opening Balance")
        c.drawRightString(575, y, inr(opening))
        y -= 13

    for r in ordered:
        lines = _wrap(r.narration, spec["wrap"])
        if y - 9 * len(lines) < 60:
            footer()
            c.showPage()
            total_pages[0] += 1
            y = column_header(height - 50)
            c.setFont("Helvetica", 7.3)
        d = r.day.strftime(spec["date"])
        c.setFont("Helvetica", 7.3)
        c.drawString(36, y, d)
        if p.layout == "hdfc_like":
            c.drawString(290, y, r.ref[-12:])
            c.drawString(362, y, r.day.strftime("%d/%m/%y"))
        elif p.layout == "sbi_like":
            c.drawString(94, y, d)
            c.drawString(338, y, r.ref[-10:])
        elif p.layout == "icici_like":
            c.drawString(322, y, r.ref[-8:])
        else:
            c.drawString(302, y, r.ref[-10:])
        c.setFont(r.font, 7.3)
        if p.layout == "kotak_like":
            c.drawRightString(470, y, inr(r.debit or r.credit))
            c.setFont("Helvetica", 7.3)
            c.drawString(482, y, "DR" if r.debit else "CR")
        else:
            debit_x, credit_x = (spec["cols"][-3][0], spec["cols"][-2][0])
            if r.debit:
                c.drawRightString(debit_x, y, inr(r.debit))
            if r.credit:
                c.drawRightString(credit_x, y, inr(r.credit))
        c.setFont("Helvetica", 7.3)
        c.drawRightString(575, y, f"{inr(r.balance)}(Cr)" if p.layout == "kotak_like" else inr(r.balance))
        for i, part in enumerate(lines):
            c.drawString(spec["narr_x"], y - 9 * i, part)
        y -= 9 * len(lines) + 4

    # Summary block
    if y < 120:
        footer()
        c.showPage()
        y = height - 60
    credits = sum((r.credit for r in rows), Decimal("0"))
    debits = sum((r.debit for r in rows), Decimal("0"))
    c.setStrokeGray(0.6)
    c.line(36, y - 4, width - 36, y - 4)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(36, y - 18, "STATEMENT SUMMARY")
    c.setFont("Helvetica", 7.5)
    for i, (k, v) in enumerate([("Opening Balance", opening), ("Total Credits", credits), ("Total Debits", debits),
                                ("Closing Balance", rows[-1].balance)]):
        c.drawString(36 + i * 135, y - 32, k)
        c.drawString(36 + i * 135, y - 42, inr(v))
    c.drawString(36, y - 58, f"Dr Count {sum(1 for r in rows if r.debit)}    Cr Count {sum(1 for r in rows if r.credit)}")
    footer()
    c.showPage()
    c.save()
    data = buf.getvalue()
    if mod_date_later:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        writer.add_metadata({"/Producer": "iLovePDF", "/Creator": "iLovePDF",
                             "/CreationDate": "D:20261001090000+05'30'", "/ModDate": "D:20261012184500+05'30'"})
        out = io.BytesIO()
        writer.write(out)
        data = out.getvalue()
    return data


def encrypt(data: bytes, password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(password, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def as_scan(data: bytes) -> bytes:
    """Rasterise every page and rebuild the PDF from images: no text layer, like a phone scan."""
    import pypdfium2 as pdfium
    from reportlab.lib.utils import ImageReader

    pdf = pdfium.PdfDocument(data)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    for i in range(len(pdf)):
        image = pdf[i].render(scale=1.4).to_pil().convert("L")
        c.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
        c.showPage()
    c.save()
    pdf.close()
    return out.getvalue()


# --------------------------------------------------------------------------- the test pack

PROFILES = [
    Profile("01_clean_upi_business", "PRIYA NAIR", "Nair Tiffin Services", "AKXPN4821Q", "9845012345", "Food and restaurants",
            "Northwind Bank", "Jayanagar, Bengaluru", "NWBK0001182", "hdfc_like", "501002345678914", 9000, cash_share=0.05,
            notes=["Steady UPI-led business, no loans. Expect a clean read and a strong band."]),
    Profile("02_password_protected", "ARJUN PATEL", "Patel Kirana Store", "BQRPP7314L", "9824033412", "Retail trade",
            "Sahyadri Bank", "Navrangpura, Ahmedabad", "SHYB0000731", "sbi_like", "390114478203356", 6500, cash_share=0.25,
            emi=8500, notes=["Password protected. Password: ARJU1985 (first four letters of name + birth year)."]),
    Profile("03_tampered_balance", "KAVYA RAO", "Rao Print Works", "CTMPR2280D", "9900122334", "Services",
            "Deccan Commercial Bank", "Banjara Hills, Hyderabad", "DCBK0002214", "icici_like", "112233445566781", 7000,
            notes=["One deposit was inflated by ₹1,50,000 after issue and the balances were not repaired. Expect FR-BAL-001 and referral to integrity review."]),
    Profile("04_tampered_font", "SURESH MENON", "Menon Auto Parts", "DLMPM9031F", "9847011223", "Wholesale trade",
            "Konkan Co-operative Bank", "Kochi, Ernakulam", "KKCB0000118", "kotak_like", "708812340019922", 11000,
            emi=15000, notes=["One deposit was retyped in a different font and every later balance repaired. Only the font trace betrays it: expect FR-FONT-001."]),
    Profile("05_edited_metadata", "MEERA IYER", "Iyer Boutique", "EMNPI5567H", "9884455667", "Textiles and apparel",
            "Northwind Bank", "T Nagar, Chennai", "NWBK0000451", "hdfc_like", "501009988776655", 5500, cash_share=0.15,
            notes=["Re-saved through an online PDF editor after it was issued (producer iLovePDF, modified 11 days after creation). Expect FR-META-001 and referral to integrity review."]),
    Profile("06_bounced_emis", "RAJESH GUPTA", "Gupta Hardware", "FNQPG1123J", "9811223344", "Retail trade",
            "Sahyadri Bank", "Karol Bagh, Delhi", "SHYB0001203", "sbi_like", "390119988001122", 5000, cash_share=0.3,
            emi=14000, bounces=3, notes=["Three EMIs returned for insufficient funds in the last three months. Expect a decline under POL-BOUNCES."]),
    Profile("07_aml_counterparty_and_cash", "FARIDA SHAIKH", "Shaikh Textiles", "GPRPS8870M", "9819876543", "Textiles and apparel",
            "Deccan Commercial Bank", "Bhiwandi, Thane", "DCBK0003390", "icici_like", "112299887766554", 8000, cash_share=0.4,
            counterparty="CRIMSON HAWALA EXCHANGE", big_cash_month=True,
            notes=["Receipts from a watchlisted counterparty and ₹11.4 lakh cash in one month. Expect AML-SAN-002 (approval blocked until compliance clears it) and AML-CTR-001."]),
    Profile("08_scanned_copy", "PRIYA NAIR", "Nair Tiffin Services", "AKXPN4821Q", "9845012345", "Food and restaurants",
            "Northwind Bank", "Jayanagar, Bengaluru", "NWBK0001182", "hdfc_like", "501002345678914", 9000, cash_share=0.05,
            notes=["A photographed/scanned copy with no text layer. Without Document AI or a vision model, upload is refused with a clear message."]),
]


def generate_pack(out_dir: Path, start: date = date(2026, 3, 1), months: int = 6) -> list[dict[str, object]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for n, p in enumerate(PROFILES, start=1):
        opening, rows = build_rows(p, start, months, seed=1000 + n)
        password = None
        producer, later = "CoreStatement Engine 7.2", False
        if p.key == "03_tampered_balance":
            victim = next(r for r in rows[len(rows) // 2:] if r.credit > 0)
            victim.credit += Decimal("150000.00")        # the balance column stays as the bank printed it
        if p.key == "04_tampered_font":
            index = next(i for i, r in enumerate(rows[len(rows) // 2:], start=len(rows) // 2) if r.credit > 0)
            rows[index].credit += Decimal("120000.00")
            rows[index].font = "Courier"                  # typed over in a different font...
            for r in rows[index:]:
                r.balance += Decimal("120000.00")         # ...and every later balance carefully repaired
        if p.key == "05_edited_metadata":
            later = True
        data = render(p, opening, rows, producer=producer, mod_date_later=later)
        if p.key == "02_password_protected":
            password = "ARJU1985"
            data = encrypt(data, password)
        if p.key == "08_scanned_copy":
            data = as_scan(data)
        name = f"{p.key}.pdf"
        (out_dir / name).write_bytes(data)
        manifest.append({
            "file": name, "bank_layout": p.layout.replace("_like", "-style"), "bank": p.bank,
            "password": password, "transactions": len(rows), "months": months,
            "period": f"{rows[0].day:%d %b %Y} to {rows[-1].day:%d %b %Y}",
            "use_these_applicant_details": {"full_name": p.holder.title(), "business_name": p.business, "pan": p.pan,
                                            "mobile": p.mobile, "industry": p.industry},
            "what_to_expect": p.notes,
        })
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["LedgerLens synthetic statement pack", "=" * 36, "",
             "Fictional banks and customers, for testing only. Upload each PDF in a new application",
             "with the applicant details listed (a different name triggers the identity check).", ""]
    for m in manifest:
        a = m["use_these_applicant_details"]
        lines += [f"{m['file']}  ({m['bank_layout']}, {m['transactions']} transactions, {m['period']})",
                  f"  Applicant: {a['full_name']} · {a['business_name']} · PAN {a['pan']} · mobile {a['mobile']} · {a['industry']}"]
        if m["password"]:
            lines.append(f"  PDF password: {m['password']}")
        lines += [f"  Expect: {' '.join(m['what_to_expect'])}", ""]
    (out_dir / "README.txt").write_text("\n".join(lines), encoding="utf-8")
    return manifest
