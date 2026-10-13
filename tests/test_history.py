"""The six-month history rule measures calendar time covered, not calendar months touched."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.cashflow.features import span_months
from app.risk.engine import apply_policy, score


@pytest.mark.parametrize("start,end,months", [
    ("2026-03-01", "2026-08-31", 6.0),   # a full six months
    ("2026-02-01", "2026-07-31", 6.0),   # February does not shorten a full six months
    ("2026-03-13", "2026-09-12", 6.0),   # mid-month to mid-month
    ("2026-03-02", "2026-08-29", 5.9),
    ("2026-03-31", "2026-08-01", 4.06),  # touches six calendar months, covers about four
])
def test_history_is_measured_in_calendar_months(start: str, end: str, months: float) -> None:
    assert span_months(date.fromisoformat(start), date.fromisoformat(end)) == months


def test_four_months_touching_six_calendar_months_is_declined_for_short_history() -> None:
    features = {"nach_returns_6m": 0, "nach_returns_3m": 0, "abb_to_inflow": 1.5, "inflow_cv": 0.1,
                "cash_deposit_share": 0.05, "low_balance_days_share": 0, "obligation_ratio": 0,
                "statement_span_months": 4.06, "inflow_trend": 0.0, "median_monthly_inflow": 300000,
                "avg_daily_balance": 450000, "monthly_surplus": 100000, "existing_emi_monthly": 0}
    result = apply_policy(features, [], score(features), Decimal("200000"), 24)
    assert result.outcome.value == "DECLINE" and "POL-HISTORY" in result.triggered_rules
