"""Late fee terms (BLUEPRINT §7.4)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .money import percent_of


@dataclass(frozen=True)
class LateFeeTerms:
    fee_type: str  # 'none' | 'flat' | 'percent'
    grace_days: int
    flat_cents: int | None = None
    percent_bp: int | None = None
    max_cents: int | None = None


def late_fee_amount(terms: LateFeeTerms, rent_cents: int) -> int:
    if terms.fee_type == "flat":
        fee = terms.flat_cents or 0
    elif terms.fee_type == "percent":
        fee = percent_of(rent_cents, terms.percent_bp or 0)
    else:
        return 0
    if terms.max_cents is not None:
        fee = min(fee, terms.max_cents)
    return max(fee, 0)


def last_grace_day(due: date, grace_days: int) -> date:
    """Rent still unpaid at the end of this day is late."""
    return due + timedelta(days=grace_days)


def is_assessable(due: date, grace_days: int, today: date) -> bool:
    return today > last_grace_day(due, grace_days)
