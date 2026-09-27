"""Dates and monthly rent periods ('YYYY-MM')."""
from __future__ import annotations

import calendar
import re
from datetime import date

_PERIOD_RE = re.compile(r"(\d{4})-(\d{2})")


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"not a valid date: {value!r} (use YYYY-MM-DD)") from None


def parse_period(period: str) -> tuple[int, int]:
    m = _PERIOD_RE.fullmatch(str(period).strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"not a valid month: {period!r} (use YYYY-MM)")
    return int(m.group(1)), int(m.group(2))


def period_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def period_start(period: str) -> date:
    y, m = parse_period(period)
    return date(y, m, 1)


def days_in_period(period: str) -> int:
    y, m = parse_period(period)
    return calendar.monthrange(y, m)[1]


def period_end(period: str) -> date:
    y, m = parse_period(period)
    return date(y, m, days_in_period(period))


def add_periods(period: str, n: int) -> str:
    y, m = parse_period(period)
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def next_period(period: str) -> str:
    return add_periods(period, 1)


def due_date(period: str, due_day: int) -> date:
    y, m = parse_period(period)
    return date(y, m, min(due_day, days_in_period(period)))
