from datetime import date

import pytest

from rental_tracker.services import leases, ledger, rent_posting, startup
from rental_tracker.services.common import LockedPeriodError, ServiceError, set_setting
from tests.conftest import make_lease


def test_future_lease_activates(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-03-01", today=date(2026, 2, 1))
    assert ledger.lease_row(conn, lid)["status"] == "future"
    startup.run_catch_up(conn, date(2026, 3, 1))
    assert ledger.lease_row(conn, lid)["status"] == "active"


def test_expired_lease_rolls_to_month_to_month(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-01-01", end="2026-03-31")
    startup.run_catch_up(conn, date(2026, 4, 2))
    assert ledger.lease_row(conn, lid)["status"] == "month_to_month"
    rent_posting.post_rent(conn, date(2026, 5, 1))
    assert conn.execute("SELECT MAX(period) FROM charges WHERE lease_id = ?", (lid,)).fetchone()[0] == "2026-05"


def test_move_out_voids_and_prorates(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-01-01", end="2026-12-31", rent=93000)
    set_setting(conn, "rent_post_days_before_due", "40")
    rent_posting.post_rent(conn, date(2026, 3, 5))  # posts March and April
    res = leases.end_lease(conn, lid, "2026-03-10")
    assert res.voided == 1 and res.credit_cents == 93000 - 30000  # 10/31 of 93,000 = 30,000
    lease = ledger.lease_row(conn, lid)
    assert lease["status"] == "ended" and lease["move_out_date"] == "2026-03-10"
    assert ledger.lease_balance(conn, lid) == 93000 * 2 + 30000


def test_renewal_carries_over_and_takes_over_unit(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-01-01", end="2026-06-30", rent=100000)
    ledger.record_deposit(conn, lid, "received", 100000, "2026-01-01")
    new_id = leases.renew_lease(conn, lid, start="2026-07-01", end="2027-06-30", rent_cents=105000,
                                today=date(2026, 5, 15))
    new = ledger.lease_row(conn, new_id)
    assert new["status"] == "future" and new["renewal_of_lease_id"] == lid
    assert ledger.deposit_held(conn, lid) == 0 and ledger.deposit_held(conn, new_id) == 100000
    startup.run_catch_up(conn, date(2026, 7, 1))
    assert ledger.lease_row(conn, lid)["status"] == "ended"
    assert ledger.lease_row(conn, lid)["move_out_date"] is None  # renewal: tenant did not move
    assert ledger.lease_row(conn, new_id)["status"] == "active"
    july = conn.execute("SELECT lease_id, amount_cents FROM charges WHERE period = '2026-07'").fetchall()
    assert [tuple(r) for r in july] == [(new_id, 105000)]
    with pytest.raises(ServiceError):
        leases.renew_lease(conn, lid, start="2026-08-01", end=None, rent_cents=1, today=date(2026, 7, 2))


def test_second_active_lease_rejected(conn, owner_id):
    lid = make_lease(conn, owner_id)
    unit_id = ledger.lease_row(conn, lid)["unit_id"]
    tid = conn.execute("SELECT tenant_id FROM lease_tenants").fetchone()[0]
    with pytest.raises(ServiceError, match="Someone already lives there"):
        leases.create_lease(conn, unit_id=unit_id, tenants=[(tid, "primary")], start="2026-02-01", end=None,
                            rent_cents=1, today=date(2026, 3, 1))


def test_nsf_and_void_rules(conn, owner_id):
    lid = make_lease(conn, owner_id)
    rent_posting.post_rent(conn, date(2026, 1, 1))
    pid = ledger.record_payment(conn, lid, 100000, "2026-01-02", "check", "1042")
    assert ledger.lease_balance(conn, lid) == 0
    ledger.void_payment(conn, pid, "NSF", nsf_fee=3500, today=date(2026, 1, 9))
    assert ledger.lease_balance(conn, lid) == 103500
    with pytest.raises(ServiceError):
        ledger.void_payment(conn, pid, "again")
    receipt = ledger.get_payment(conn, pid)["receipt_number"]
    assert receipt == "R-2026-000001"


def test_books_lock_blocks_changes(conn, owner_id):
    lid = make_lease(conn, owner_id)
    pid = ledger.record_payment(conn, lid, 1000, "2026-01-02", "cash")
    set_setting(conn, "books_locked_through", "2026-01-31")
    with pytest.raises(LockedPeriodError):
        ledger.void_payment(conn, pid, "oops")
    with pytest.raises(LockedPeriodError):
        ledger.record_payment(conn, lid, 1000, "2026-01-15", "cash")


def test_deposits(conn, owner_id):
    lid = make_lease(conn, owner_id)
    rent_posting.post_rent(conn, date(2026, 1, 1))
    ledger.record_deposit(conn, lid, "received", 100000, "2026-01-01")
    with pytest.raises(ServiceError):
        ledger.record_deposit(conn, lid, "refund", 200000, "2026-02-01")
    ledger.record_deposit(conn, lid, "applied_to_balance", 100000, "2026-02-01")
    assert ledger.deposit_held(conn, lid) == 0 and ledger.lease_balance(conn, lid) == 0
    txn = conn.execute("SELECT id FROM deposit_transactions WHERE txn_type = 'applied_to_balance'").fetchone()[0]
    ledger.void_deposit(conn, txn, "entered by mistake")
    assert ledger.deposit_held(conn, lid) == 100000 and ledger.lease_balance(conn, lid) == 100000


def test_fill_rent_paid_covers_partly_paid_months(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-01-01", end=None, rent=100000, today=date(2026, 3, 15))
    assert len(ledger.unpaid_rent(conn, lid, date(2026, 3, 15))) == 3
    ledger.record_payment(conn, lid, 50000, "2026-02-03", "cash")  # half a month, applied to January
    ledger.add_charge(conn, lid, "other", 20000, "2026-03-15", "Debt")
    n, total = ledger.fill_rent_paid(conn, lid, date(2026, 12, 31), date(2026, 3, 15), "gcash")
    assert (n, total) == (3, 50000 + 100000 + 100000)
    dates = [r[0] for r in conn.execute("SELECT received_date FROM payments WHERE method = 'gcash' ORDER BY id")]
    assert dates == ["2026-01-01", "2026-02-01", "2026-03-01"]  # on each due date, never in the future
    assert ledger.lease_balance(conn, lid) == 20000  # the debt is still owed
    assert ledger.fill_rent_paid(conn, lid, date(2026, 3, 15), date(2026, 3, 15)) == (0, 0)


def test_bill_from_move_in_rebuilds_rent(conn, owner_id):
    lid = make_lease(conn, owner_id, start="2026-03-01", end=None, rent=100000, today=date(2026, 3, 15),
                     billing_start="2026-03-01")
    ledger.record_payment(conn, lid, 100000, "2026-03-02", "cash")
    leases.bill_from_move_in(conn, lid, "2026-01-16", date(2026, 3, 15))
    rows = conn.execute("SELECT period, amount_cents FROM charges WHERE lease_id = ? AND charge_type = 'rent' "
                        "ORDER BY period", (lid,)).fetchall()
    assert [r[0] for r in rows] == ["2026-01", "2026-02", "2026-03"]
    assert rows[0][1] < 100000  # half of January, prorated
    assert conn.execute("SELECT COUNT(*) FROM payments WHERE lease_id = ?", (lid,)).fetchone()[0] == 1
    lease = leases.get_lease(conn, lid)
    assert (lease["start_date"], lease["move_in_date"], lease["billing_start_date"]) == ("2026-01-16", "2026-01-16", None)
    leases.bill_from_move_in(conn, lid, "2026-04-01", date(2026, 3, 15))  # a later date: not moved in yet
    assert leases.get_lease(conn, lid)["status"] == "future"
    assert conn.execute("SELECT COUNT(*) FROM charges WHERE lease_id = ? AND charge_type = 'rent'", (lid,)).fetchone()[0] == 0
