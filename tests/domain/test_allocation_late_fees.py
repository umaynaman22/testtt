from datetime import date, timedelta

from hypothesis import given, strategies as st

from rental_tracker.domain.allocation import (LedgerCharge, LedgerPayment, aging, allocate,
                                              oldest_unpaid_due, unpaid_for_period)
from rental_tracker.domain.late_fees import LateFeeTerms, is_assessable, late_fee_amount

D = date


def rent(i, period, due, amount=100000):
    return LedgerCharge(i, "rent", amount, due, period)


def test_payment_toward_a_line_pays_that_line_first():
    charges = [rent(1, "2026-01", D(2026, 1, 1)), rent(2, "2026-02", D(2026, 2, 1)), rent(3, "2026-03", D(2026, 3, 1))]
    alloc = allocate(charges, [LedgerPayment(1, 100000, D(2026, 2, 1), charge_id=2)])
    assert alloc.unpaid == {1: 100000, 2: 0, 3: 100000}  # February marked paid, January still owed
    alloc = allocate(charges, [LedgerPayment(1, 130000, D(2026, 2, 1), charge_id=2)])  # extra goes oldest first
    assert alloc.unpaid == {1: 70000, 2: 0, 3: 100000}
    alloc = allocate(charges, [LedgerPayment(1, 40000, D(2026, 3, 1), charge_id=3)])  # part of a line
    assert alloc.unpaid[3] == 60000 and alloc.unpaid[1] == 100000
    alloc = allocate(charges, [LedgerPayment(1, 50000, D(2026, 2, 1), charge_id=99)])  # unknown line: oldest first
    assert alloc.unpaid[1] == 50000
    assert unpaid_for_period(charges, [LedgerPayment(1, 100000, D(2026, 2, 1), charge_id=2)], "2026-02",
                             D(2026, 2, 6)) == 0  # paid on time, so no late fee for February


def test_oldest_first_and_rent_before_fees():
    charges = [rent(1, "2026-01", D(2026, 1, 1)),
               LedgerCharge(2, "late_fee", 5000, D(2026, 1, 1), "2026-01"),
               rent(3, "2026-02", D(2026, 2, 1))]
    alloc = allocate(charges, [LedgerPayment(1, 120000, D(2026, 2, 2))])
    assert alloc.unpaid == {1: 0, 2: 0, 3: 85000}
    alloc = allocate(charges, [LedgerPayment(1, 100000, D(2026, 2, 2))])
    assert alloc.unpaid[1] == 0 and alloc.unpaid[2] == 5000  # fee paid after rent on same due date


def test_as_of_ignores_later_money():
    charges = [rent(1, "2026-01", D(2026, 1, 1))]
    payments = [LedgerPayment(1, 40000, D(2026, 1, 3)), LedgerPayment(2, 60000, D(2026, 1, 10))]
    assert unpaid_for_period(charges, payments, "2026-01", as_of=D(2026, 1, 6)) == 60000
    assert unpaid_for_period(charges, payments, "2026-01", as_of=D(2026, 1, 10)) == 0


def test_credits_count_as_funds():
    charges = [rent(1, "2026-01", D(2026, 1, 1)), LedgerCharge(2, "credit", -30000, D(2026, 1, 1))]
    assert allocate(charges, []).unpaid[1] == 70000
    assert sum(c.amount_cents for c in charges) == 70000


def test_aging_buckets_and_credit():
    today = D(2026, 5, 1)
    charges = [rent(1, "2026-01", D(2026, 1, 1)), rent(2, "2026-03", D(2026, 3, 15)),
               rent(3, "2026-04", D(2026, 4, 20)), rent(4, "2026-05", D(2026, 5, 1))]
    a = aging(charges, [LedgerPayment(1, 50000, D(2026, 1, 5))], today)
    assert a["90_plus"] == 50000 and a["31_60"] == 100000 and a["1_30"] == 100000
    assert a["current"] == 100000 and a["past_due"] == 250000 and a["credit"] == 0
    assert oldest_unpaid_due(charges, [LedgerPayment(1, 100000, D(2026, 1, 5))]) == D(2026, 3, 15)
    over = aging([rent(1, "2026-01", D(2026, 1, 1))], [LedgerPayment(1, 150000, D(2026, 1, 1))], today)
    assert over["credit"] == 50000 and over["past_due"] == 0


def test_late_fee_amounts():
    assert late_fee_amount(LateFeeTerms("flat", 5, flat_cents=5000), 140000) == 5000
    assert late_fee_amount(LateFeeTerms("percent", 5, percent_bp=500), 140000) == 7000
    assert late_fee_amount(LateFeeTerms("percent", 5, percent_bp=1000, max_cents=10000), 140000) == 10000
    assert late_fee_amount(LateFeeTerms("none", 5), 140000) == 0


def test_grace_period_boundary():
    due = D(2026, 1, 1)
    assert not is_assessable(due, 5, D(2026, 1, 6))  # last grace day
    assert is_assessable(due, 5, D(2026, 1, 7))


charge_st = st.builds(
    lambda i, t, amt, day: LedgerCharge(i, t, amt if t != "credit" else -amt, D(2026, 1, 1) + timedelta(days=day)),
    st.integers(1, 10**6), st.sampled_from(["rent", "late_fee", "credit", "utility"]),
    st.integers(1, 10**7), st.integers(0, 400))
payment_st = st.builds(lambda i, amt, day: LedgerPayment(i, amt, D(2026, 1, 1) + timedelta(days=day)),
                       st.integers(1, 10**6), st.integers(1, 10**7), st.integers(0, 400))


@given(st.lists(charge_st, max_size=30, unique_by=lambda c: c.id), st.lists(payment_st, max_size=30))
def test_allocation_invariants(charges, payments):
    alloc = allocate(charges, payments)
    unpaid_total = sum(alloc.unpaid.values())
    # balance = what is still owed minus money not yet applied to anything
    assert unpaid_total - alloc.unapplied_credit == (sum(c.amount_cents for c in charges)
                                                    - sum(p.amount_cents for p in payments))
    assert all(0 <= alloc.unpaid[c.id] <= c.amount_cents for c in charges if c.amount_cents > 0)
    assert unpaid_total == 0 or alloc.unapplied_credit == 0  # never both owed and holding credit
    a = aging(charges, payments, D(2027, 6, 1))
    assert sum(a[k] for k in ("current", "1_30", "31_60", "61_90", "90_plus")) == unpaid_total
