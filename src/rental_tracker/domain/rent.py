"""Which recurring charges a lease should have been billed (BLUEPRINT §7.3)."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date

from .periods import days_in_period, due_date, next_period, period_of
from .proration import occupied_days, prorate


@dataclass(frozen=True)
class PlannedCharge:
    period: str
    due_date: date
    amount_cents: int
    prorated_days: int | None  # None = full month


def rent_in_effect(base_cents: int, changes: Iterable[tuple[date, int]], on: date) -> int:
    """Rent on a given day: the latest change effective on or before it, else the base rent."""
    best: tuple[date, int] | None = None
    for effective, cents in changes:
        if effective <= on and (best is None or effective > best[0]):
            best = (effective, cents)
    return best[1] if best else base_cents


def plan_charges(*, start: date, end: date | None, due_day: int, base_cents: int,
                 changes: Iterable[tuple[date, int]] = (), prorate_partial: bool = True,
                 method: str = "actual_days", from_period: str | None = None,
                 through: date) -> Iterator[PlannedCharge]:
    """Monthly charges from ``from_period`` (or the start month) whose due date is <= ``through``.

    A partial first month is due on the start date. ``end`` None means open-ended.
    """
    changes = list(changes)
    p = max(from_period or period_of(start), period_of(start))
    last = period_of(end) if end else None
    while last is None or p <= last:
        due = max(due_date(p, due_day), start)
        if due > through:
            break
        cents = rent_in_effect(base_cents, changes, due)
        days = occupied_days(p, start, end)
        partial = days < days_in_period(p)
        amount = prorate(cents, p, start, end, method) if (partial and prorate_partial) else cents
        if amount > 0:
            yield PlannedCharge(p, due, amount, days if (partial and prorate_partial) else None)
        p = next_period(p)
