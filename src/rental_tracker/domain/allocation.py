"""Applying payments to charges, and aging (BLUEPRINT §7.5).

Nothing here is stored: it is recomputed from the ledger whenever needed, so
correcting an old entry updates everything consistently.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

ORDERS = ("oldest_first_rent_before_fees", "oldest_first")
FEE_TYPES = frozenset({"late_fee", "nsf_fee", "legal_fee"})
BUCKETS = ("current", "1_30", "31_60", "61_90", "90_plus")


@dataclass(frozen=True)
class LedgerCharge:
    id: int
    charge_type: str
    amount_cents: int  # negative for credits
    due_date: date
    period: str | None = None


@dataclass(frozen=True)
class LedgerPayment:
    id: int
    amount_cents: int
    received_date: date


@dataclass
class Allocation:
    unpaid: dict[int, int] = field(default_factory=dict)  # charge id -> unpaid cents
    unapplied_credit: int = 0


def _sort_key(order: str):
    if order == "oldest_first":
        return lambda c: (c.due_date, c.id)
    if order == "oldest_first_rent_before_fees":
        return lambda c: (c.due_date, 1 if c.charge_type in FEE_TYPES else 0, c.id)
    raise ValueError(f"unknown payment application order: {order}")


def allocate(charges: Iterable[LedgerCharge], payments: Iterable[LedgerPayment],
             order: str = "oldest_first_rent_before_fees", as_of: date | None = None) -> Allocation:
    """Apply payments and credits to positive charges in priority order.

    With ``as_of``, only charges due and money received on or before that day count.
    """
    charges = [c for c in charges if as_of is None or c.due_date <= as_of]
    funds = sum(p.amount_cents for p in payments if as_of is None or p.received_date <= as_of)
    funds += sum(-c.amount_cents for c in charges if c.amount_cents < 0)
    result = Allocation()
    for c in sorted((c for c in charges if c.amount_cents > 0), key=_sort_key(order)):
        applied = min(funds, c.amount_cents)
        funds -= applied
        result.unpaid[c.id] = c.amount_cents - applied
    result.unapplied_credit = funds
    return result


def balance(charges: Iterable[LedgerCharge], payments: Iterable[LedgerPayment]) -> int:
    return sum(c.amount_cents for c in charges) - sum(p.amount_cents for p in payments)


def bucket_for(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "1_30"
    if days_past_due <= 60:
        return "31_60"
    if days_past_due <= 90:
        return "61_90"
    return "90_plus"


def aging(charges: Iterable[LedgerCharge], payments: Iterable[LedgerPayment], today: date,
          order: str = "oldest_first_rent_before_fees") -> dict[str, int]:
    """Unpaid amounts by days past due, plus 'credit' (unapplied money) and 'past_due'."""
    charges = list(charges)
    alloc = allocate(charges, payments, order)
    out = dict.fromkeys(BUCKETS, 0)
    for c in charges:
        unpaid = alloc.unpaid.get(c.id, 0)
        if unpaid:
            out[bucket_for((today - c.due_date).days)] += unpaid
    out["credit"] = alloc.unapplied_credit
    out["past_due"] = sum(out[b] for b in BUCKETS if b != "current")
    return out


def unpaid_for_period(charges: Iterable[LedgerCharge], payments: Iterable[LedgerPayment],
                      period: str, as_of: date, order: str = "oldest_first_rent_before_fees",
                      charge_types: frozenset[str] = frozenset({"rent"})) -> int:
    charges = list(charges)
    alloc = allocate(charges, payments, order, as_of)
    return sum(alloc.unpaid.get(c.id, 0) for c in charges
               if c.period == period and c.charge_type in charge_types)


def oldest_unpaid_due(charges: Iterable[LedgerCharge], payments: Iterable[LedgerPayment],
                      order: str = "oldest_first_rent_before_fees") -> date | None:
    charges = list(charges)
    alloc = allocate(charges, payments, order)
    dues = [c.due_date for c in charges if alloc.unpaid.get(c.id, 0) > 0]
    return min(dues) if dues else None
