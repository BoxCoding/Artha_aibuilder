"""Locale-aware display formatting shared by memo, explanations and UI payloads."""
from __future__ import annotations

from decimal import Decimal


def inr(amount: Decimal | float | int, decimals: int = 0) -> str:
    """Indian digit grouping: 12,34,567."""
    value = Decimal(str(amount))
    sign = "-" if value < 0 else ""
    text = f"{abs(value):.{decimals}f}"
    whole, _, frac = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"{sign}{whole}" + (f".{frac}" if frac else "")


def pct(ratio: float, decimals: int = 0) -> str:
    return f"{ratio * 100:.{decimals}f}%"


def emi(principal: Decimal, annual_rate: Decimal, months: int) -> Decimal:
    r = annual_rate / Decimal(12)
    if r == 0:
        return (principal / months).quantize(Decimal("1"))
    return (principal * r / (1 - (1 + r) ** (-months))).quantize(Decimal("1"))
