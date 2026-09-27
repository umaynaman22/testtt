from datetime import date

from rental_tracker.services import late_fees, leases, ledger, rent_posting, startup
from rental_tracker.services.common import set_setting
from tests.conftest import make_lease


def rents(conn, lid):
    return [(r["period"], r["amount_cents"], r["due_date"]) for r in conn.execute(
        "SELECT * FROM charges WHERE lease_id = ? AND charge_type = 'rent' AND voided_at IS NULL ORDER BY period", (lid,))]


def test_posting_is_idempotent_and_prorates(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-01-15", end="2026-06-30", rent=100000, today=date(2026, 1, 15))
    assert len(rents(conn, lid)) == 1  # the prorated first month is billed as soon as the lease is created
    r1 = rent_posting.post_rent(conn, date(2026, 3, 10))
    r2 = rent_posting.post_rent(conn, date(2026, 3, 10))
    assert r1.posted == 2 and r2.posted == 0
    assert rents(conn, lid) == [("2026-01", 54839, "2026-01-15"), ("2026-02", 100000, "2026-02-01"),
                                ("2026-03", 100000, "2026-03-01")]
    rent_posting.post_rent(conn, date(2026, 12, 1))
    assert [p for p, _, _ in rents(conn, lid)][-1] == "2026-06"  # stops at lease end


def test_lookahead_and_rent_change(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-01-01", end=None)
    leases.add_rent_change(conn, lid, "2026-03-01", 110000)
    set_setting(conn, "rent_post_days_before_due", "5")
    rent_posting.post_rent(conn, date(2026, 2, 25))
    assert rents(conn, lid)[-1] == ("2026-03", 110000, "2026-03-01")


def test_billing_start_skips_history(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2019-05-01", end=None, today=date(2026, 9, 27), billing_start="2026-09-01")
    rent_posting.post_rent(conn, date(2026, 9, 27))
    assert [p for p, _, _ in rents(conn, lid)] == ["2026-09"]


def test_recurring_charges(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-01-01", end=None)
    leases.add_recurring_charge(conn, lid, charge_type="pet_rent", description="Pet rent", amount_cents=3500,
                                start="2026-02-01")
    rent_posting.post_rent(conn, date(2026, 3, 1))
    rows = conn.execute("SELECT period, amount_cents FROM charges WHERE charge_type = 'pet_rent' ORDER BY period").fetchall()
    assert [tuple(r) for r in rows] == [("2026-02", 3500), ("2026-03", 3500)]


def test_late_fee_review_approve_and_waive(conn, owner_id):
    lid = make_lease(conn, owner_id, late_fee_type="flat", late_fee_flat_cents=5000, late_fee_grace_days=5)
    rent_posting.post_rent(conn, date(2026, 2, 10))
    ledger.record_payment(conn, lid, 100000, "2026-01-05", "check")  # January on time (within grace)
    ledger.record_payment(conn, lid, 40000, "2026-02-03", "check")   # February short
    cands = late_fees.find_candidates(conn, date(2026, 2, 6))
    assert cands == []  # still inside grace on the 6th
    cands = late_fees.find_candidates(conn, date(2026, 2, 7))
    assert [(c.period, c.unpaid_cents, c.fee_cents) for c in cands] == [("2026-02", 60000, 5000)]
    assert late_fees.apply(conn, [cands[0].key], "approve", date(2026, 2, 7)) == 1
    assert late_fees.find_candidates(conn, date(2026, 2, 7)) == []
    assert ledger.lease_balance(conn, lid) == 65000

    rent_posting.post_rent(conn, date(2026, 3, 10))
    cands = late_fees.find_candidates(conn, date(2026, 3, 10))
    assert [c.period for c in cands] == ["2026-03"]
    late_fees.apply(conn, [cands[0].key], "waive", date(2026, 3, 10))
    assert late_fees.find_candidates(conn, date(2026, 3, 10)) == []  # waived stays waived
    assert ledger.lease_balance(conn, lid) == 165000


def test_percent_late_fee_auto_mode(conn, owner_id):
    make_lease(conn, owner_id, late_fee_type="percent", late_fee_percent_bp=500, late_fee_grace_days=3)
    set_setting(conn, "late_fee_mode", "auto")
    result = startup.run_catch_up(conn, date(2026, 1, 10))
    assert result.rent_posted == 0 and result.late_fees_posted == 1  # January was billed at creation
    fee = conn.execute("SELECT amount_cents FROM charges WHERE charge_type = 'late_fee'").fetchone()[0]
    assert fee == 5000


def test_books_lock_skips_posting(conn, owner_id):
    set_setting(conn, "books_locked_through", "2026-01-31")
    lid = make_lease(conn, owner_id, start="2026-01-01", end=None, today=date(2026, 2, 1))
    assert [p for p, _, _ in rents(conn, lid)] == ["2026-02"]  # January is locked, so it is skipped
    assert rent_posting.post_rent(conn, date(2026, 2, 1)).posted == 0
