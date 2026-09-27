"""Partial-month rent (BLUEPRINT §7.3)."""
from __future__ import annotations

from datetime import date

from .money import round_div
from .periods import days_in_period, period_end, period_start

METHODS = ("actual_days", "thirty_day_month")


def occupied_days(period: str, start: date | None, end: date | None) -> int:
    """Days of ``period`` inside [start, end] (inclusive; None = open)."""
    lo = max(period_start(period), start) if start else period_start(period)
    hi = min(period_end(period), end) if end else period_end(period)
    return max(0, (hi - lo).days + 1)


def prorate(monthly_cents: int, period: str, start: date | None, end: date | None,
            method: str = "actual_days") -> int:
    days = occupied_days(period, start, end)
    total = days_in_period(period)
    if days == 0:
        return 0
    if days >= total:
        return monthly_cents
    if method == "thirty_day_month":
        return min(monthly_cents, round_div(monthly_cents * min(days, 30), 30))
    if method != "actual_days":
        raise ValueError(f"unknown proration method: {method}")
    return round_div(monthly_cents * days, total)
