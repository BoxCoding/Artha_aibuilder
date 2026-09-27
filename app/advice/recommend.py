"""Personalised recommendations for the customer, computed from their own statement.

Three kinds, all deterministic and all derived from numbers the customer can check:

* **When to pay.** The EMI date that falls just after the business usually receives most of its money
  in a month, so the instalment is least likely to bounce.
* **What would work now.** If the request was not approved as asked, the amount and tenure the
  unmodified policy engine *would* approve (from the structuring search).
* **What to change.** The smallest set of changes to their banking behaviour that would move them up
  a risk band, as concrete targets ("keep an average balance above ₹45,000").

Text comes from fixed templates in English, Hindi and Indonesian. No model writes these.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from app.cashflow.categorise import BUSINESS_CREDIT
from app.domain.formatting import inr
from app.domain.models import Case

T = {
    "emi_date": {
        "en": "Choose the {day} of the month as your EMI date. About {share}% of your business receipts usually arrive by then, so the instalment is less likely to bounce.",
        "hi": "EMI की तारीख महीने की {day} तारीख रखें। तब तक आपकी व्यापारिक आय का लगभग {share}% आ चुका होता है, इसलिए किश्त लौटने की संभावना कम रहती है।",
        "id": "Pilih tanggal {day} setiap bulan sebagai tanggal cicilan. Sekitar {share}% penerimaan usaha Anda biasanya sudah masuk saat itu, sehingga cicilan lebih kecil kemungkinannya gagal.",
    },
    "alternative": {
        "en": "{amount} over {tenure} months fits our lending policy today, with an instalment of about {emi} a month.",
        "hi": "{tenure} महीनों के लिए {amount} आज हमारी नीति के अनुसार संभव है, लगभग {emi} मासिक किश्त के साथ।",
        "id": "{amount} selama {tenure} bulan sesuai kebijakan kami saat ini, dengan cicilan sekitar {emi} per bulan.",
    },
    "abb_to_inflow": {
        "en": "Keep an average balance of at least {target} (about {pct}% of a typical month's receipts). Today it is about {current}.",
        "hi": "खाते में औसतन कम से कम {target} रखें (एक सामान्य महीने की आय का लगभग {pct}%)। अभी यह लगभग {current} है।",
        "id": "Jaga saldo rata-rata minimal {target} (sekitar {pct}% dari penerimaan sebulan). Saat ini sekitar {current}.",
    },
    "cash_deposit_share": {
        "en": "Take more payments by UPI or bank transfer: aim for cash to be under {pct}% of your receipts (it is {current_pct}% now).",
        "hi": "ज़्यादा भुगतान UPI या बैंक ट्रांसफ़र से लें: नकद आपकी आय के {pct}% से कम रखने का लक्ष्य रखें (अभी {current_pct}% है)।",
        "id": "Terima lebih banyak pembayaran lewat UPI atau transfer: usahakan tunai di bawah {pct}% dari penerimaan (sekarang {current_pct}%).",
    },
    "low_balance_days_share": {
        "en": "Avoid letting the balance drop very low: aim for no more than {pct}% of days near zero (it is {current_pct}% now).",
        "hi": "बैलेंस को बहुत कम न होने दें: लगभग शून्य बैलेंस वाले दिन {pct}% से ज़्यादा न हों (अभी {current_pct}% हैं)।",
        "id": "Hindari saldo terlalu rendah: usahakan hari dengan saldo hampir nol tidak lebih dari {pct}% (sekarang {current_pct}%).",
    },
    "nach_returns_6m": {
        "en": "Make sure no instalment is returned unpaid: bring returned EMIs down to {target} over the next six months (there were {current}).",
        "hi": "ध्यान रखें कि कोई किश्त बिना भुगतान वापस न लौटे: अगले छह महीनों में लौटी EMI {target} तक लाएँ (अभी {current} थीं)।",
        "id": "Pastikan tidak ada cicilan yang gagal: turunkan cicilan gagal menjadi {target} dalam enam bulan ke depan (sebelumnya {current}).",
    },
}
ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 21: "21st", 22: "22nd", 23: "23rd", 31: "31st"}


def _ordinal(day: int, lang: str) -> str:
    if lang != "en":
        return str(day)
    return ORDINAL.get(day, f"{day}th")


def best_emi_day(case: Case) -> tuple[int, int] | None:
    """The day of month by which the largest share of a month's business receipts has usually arrived,
    choosing the earliest day that captures at least 60% (clamped to the 5th-25th, the dates NACH offers)."""
    by_day: dict[int, Decimal] = defaultdict(Decimal)
    total = Decimal("0")
    for t in case.transactions:
        if t.credit > 0 and t.category in BUSINESS_CREDIT:
            by_day[t.txn_date.day] += t.credit
            total += t.credit
    if total <= 0:
        return None
    running = Decimal("0")
    for day in range(1, 32):
        running += by_day.get(day, Decimal("0"))
        if running / total >= Decimal("0.6"):
            chosen = min(25, max(5, day + 1))
            captured = sum((v for d, v in by_day.items() if d < chosen), Decimal("0")) / total
            return chosen, int(captured * 100)
    return None


def build(case: Case, lang: str) -> dict[str, Any]:
    lang = lang if lang in ("en", "hi", "id") else "en"
    items: list[dict[str, str]] = []
    emi_day = best_emi_day(case)
    if emi_day:
        items.append({"kind": "emi_date", "text": T["emi_date"][lang].format(day=_ordinal(emi_day[0], lang), share=emi_day[1])})

    decision = case.decision
    approved_as_asked = bool(decision and decision.final_outcome.value.startswith("APPROVE")
                             and decision.approved_amount and decision.approved_amount >= case.requested_amount)
    if not approved_as_asked:
        for option in (case.structuring or {}).get("viable", [])[:2]:
            items.append({"kind": "alternative", "text": T["alternative"][lang].format(
                amount=f"₹{option['amount_display']}", tenure=option["tenure_months"],
                emi=f"₹{option['monthly_instalment_display']}")})

    inflow = float(case.features.get("median_monthly_inflow", 0) or 0)
    for step in case.improvement_plan[:3]:
        feature = step.feature
        if feature == "abb_to_inflow" and inflow:
            items.append({"kind": "target", "text": T[feature][lang].format(
                target=f"₹{inr(Decimal(round(step.target * inflow)))}", pct=round(step.target * 100),
                current=f"₹{inr(Decimal(round(step.current * inflow)))}")})
        elif feature in ("cash_deposit_share", "low_balance_days_share"):
            items.append({"kind": "target", "text": T[feature][lang].format(
                pct=round(step.target * 100), current_pct=round(step.current * 100))})
        elif feature == "nach_returns_6m":
            items.append({"kind": "target", "text": T[feature][lang].format(target=int(step.target), current=int(step.current))})
    return {"language": lang, "items": items}
