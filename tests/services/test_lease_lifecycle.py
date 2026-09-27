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


def test_second_active_lease_rejected(conn, owner_id):
    lid = make_lease(conn, owner_id)
    unit_id = ledger.lease_row(conn, lid)["unit_id"]
    tid = conn.execute("SELECT tenant_id FROM lease_tenants").fetchone()[0]
    with pytest.raises(ServiceError, match="Someone already lives there"):
        leases.create_lease(conn, unit_id=unit_id, tenants=[(tid, "primary")], start="2026-02-01", end=None,
                            rent_cents=1, today=date(2026, 3, 1))


def test_payment_receipt_numbers_and_methods(conn, owner_id):
    lid = make_lease(conn, owner_id)
    rent_posting.post_rent(conn, date(2026, 1, 1))
    pid = ledger.record_payment(conn, lid, 100000, "2026-01-02", "check", notes="cheque 1042")
    assert ledger.lease_balance(conn, lid) == 0
    assert ledger.get_payment(conn, pid)["receipt_number"] == "R-2026-000001"
    assert ledger.record_payment(conn, lid, 1, "2026-01-03", None)  # blank method means "other"
    with pytest.raises(ServiceError, match="Unknown payment method"):
        ledger.record_payment(conn, lid, 1, "2026-01-03", "money_order")  # old methods can't be chosen now


def test_books_lock_blocks_changes(conn, owner_id):
    lid = make_lease(conn, owner_id)
    pid = ledger.record_payment(conn, lid, 1000, "2026-01-02", "cash")
    set_setting(conn, "books_locked_through", "2026-01-31")
    with pytest.raises(LockedPeriodError):
        ledger.edit_line(conn, lid, "payment", pid, amount=2000)
    with pytest.raises(LockedPeriodError):
        ledger.record_payment(conn, lid, 1000, "2026-01-15", "cash")


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


def test_pay_line_full_and_partial(conn, owner_id):
    today = date(2026, 3, 15)
    lid = make_lease(conn, owner_id, start="2026-01-01", end=None, rent=100000, today=today)
    rent = {r["period"]: r["id"] for r in conn.execute(
        "SELECT id, period FROM charges WHERE lease_id = ? AND charge_type = 'rent'", (lid,))}
    ledger.record_payment(conn, lid, 1, "2026-01-02", "gcash")  # the tenant's usual method
    res = ledger.pay_line(conn, lid, rent["2026-02"], None, today)  # the whole line
    assert res["amount"] == 100000 and res["left"] == 0
    pay = conn.execute("SELECT * FROM payments WHERE id = ?", (res["payment_id"],)).fetchone()
    assert (pay["received_date"], pay["method"], pay["charge_id"]) == ("2026-02-01", "gcash", rent["2026-02"])
    entries = {e["id"]: e for e in ledger.ledger_entries(conn, lid) if e["kind"] == "charge"}
    assert entries[rent["2026-02"]]["unpaid"] == 0 and entries[rent["2026-01"]]["unpaid"] == 99999
    assert any(e["description"] == "Payment — GCash, for Rent 2026-02" for e in ledger.ledger_entries(conn, lid))
    res = ledger.pay_line(conn, lid, rent["2026-03"], 30000, today)  # part of it
    assert res["left"] == 70000
    with pytest.raises(ServiceError, match="more than"):
        ledger.pay_line(conn, lid, rent["2026-03"], 70001, today)
    with pytest.raises(ServiceError, match="already paid"):
        ledger.pay_line(conn, lid, rent["2026-02"], None, today)
    conn.execute("DELETE FROM charges WHERE id = ?", (rent["2026-02"],))  # line removed: payment stays, unlinked
    assert conn.execute("SELECT charge_id FROM payments WHERE id = ?", (pay["id"],)).fetchone()[0] is None
