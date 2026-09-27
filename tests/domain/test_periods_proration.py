from datetime import date

import pytest

from rental_tracker.domain.periods import add_periods, due_date, period_end, parse_period
from rental_tracker.domain.proration import occupied_days, prorate


def test_period_math():
    assert add_periods("2026-12", 1) == "2027-01"
    assert add_periods("2026-01", -1) == "2025-12"
    assert period_end("2028-02") == date(2028, 2, 29)
    assert due_date("2026-02", 28) == date(2026, 2, 28)
    with pytest.raises(ValueError):
        parse_period("2026-13")


@pytest.mark.parametrize("period,start,end,expected", [
    ("2026-01", date(2026, 1, 15), None, 100000 * 17 // 31 + 1),  # 54838.7 -> 54839
    ("2026-02", date(2026, 2, 1), date(2026, 2, 14), 50000),      # 14/28
    ("2028-02", date(2028, 2, 15), None, round(100000 * 15 / 29)),  # leap year
    ("2026-04", date(2026, 3, 1), None, 100000),                   # full month
    ("2026-04", date(2026, 5, 1), None, 0),                        # not occupied
    ("2026-04", date(2026, 4, 30), date(2026, 4, 30), round(100000 / 30)),
])
def test_prorate_actual_days(period, start, end, expected):
    assert prorate(100000, period, start, end) == expected


def test_prorate_thirty_day_month():
    assert prorate(90000, "2026-01", date(2026, 1, 17), None, "thirty_day_month") == 45000  # 15 days
    assert prorate(90000, "2026-01", date(2026, 1, 2), None, "thirty_day_month") == 90000   # 30 of 31 days, capped


def test_occupied_days():
    assert occupied_days("2026-03", date(2026, 3, 10), date(2026, 3, 20)) == 11
