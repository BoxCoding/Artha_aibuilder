"""Rules-first transaction categorisation. Only unmatched narrations are sent to a model."""
from __future__ import annotations

import re

from app.domain.models import Transaction

CATEGORIES = (
    "upi_credit", "neft_imps_credit", "card_settlement", "cash_deposit", "loan_disbursal", "self_transfer",
    "nach_return", "emi_debit", "return_charges", "supplier_payment", "rent", "utilities", "staff_salary",
    "cash_withdrawal", "bank_charges", "tax_payment", "other_credit", "other_debit",
)
BUSINESS_CREDIT = {"upi_credit", "neft_imps_credit", "card_settlement", "cash_deposit", "other_credit"}
DIGITAL_CREDIT = {"upi_credit", "neft_imps_credit", "card_settlement"}

_CREDIT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nach_return", re.compile(r"\b(NACH|ECS|ACH)\b.*\b(RTN|RETURN|REV)\b")),
    ("loan_disbursal", re.compile(r"\bLOAN\s*DISB")),
    ("self_transfer", re.compile(r"\bSELF\b")),
    ("upi_credit", re.compile(r"\bUPI\b")),
    ("card_settlement", re.compile(r"\b(POS|CARD)\s*SETTL")),
    ("neft_imps_credit", re.compile(r"\b(NEFT|IMPS|RTGS)\b")),
    ("cash_deposit", re.compile(r"\bCASH\s*DEP")),
)
_DEBIT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("return_charges", re.compile(r"\b(RTN|RETURN)\s*CHG")),
    ("emi_debit", re.compile(r"\b(NACH|ECS|ACH)\s*DR\b|\bEMI\b")),
    ("rent", re.compile(r"\bRENT\b")),
    ("utilities", re.compile(r"ELECTRICITY|WATER BOARD|BROADBAND|\bGAS\b|MOBILE BILL")),
    ("staff_salary", re.compile(r"SALARY|WAGES")),
    ("cash_withdrawal", re.compile(r"\bATM\b|CASH WDL")),
    ("tax_payment", re.compile(r"\bGST\b|\bTDS\b|INCOME TAX")),
    ("bank_charges", re.compile(r"\bCHG\b|CHARGES")),
    ("self_transfer", re.compile(r"\bSELF\b")),
    ("supplier_payment", re.compile(r"\b(UPI|NEFT|IMPS|RTGS)\b")),
)


def categorise_by_rules(txn: Transaction) -> str | None:
    text = txn.narration.upper()
    rules = _CREDIT_RULES if txn.credit > 0 else _DEBIT_RULES
    return next((cat for cat, rx in rules if rx.search(text)), None)


def apply_rules(transactions: list[Transaction]) -> list[Transaction]:
    """Categorise in place; returns the transactions that still need a model or a default."""
    unmatched: list[Transaction] = []
    for t in transactions:
        cat = categorise_by_rules(t)
        if cat:
            t.category, t.category_source = cat, "RULE"
        else:
            t.category = "other_credit" if t.credit > 0 else "other_debit"
            unmatched.append(t)
    return unmatched
