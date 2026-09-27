from datetime import date

import pytest

from rental_tracker.services import deletion, late_fees, ledger, rent_posting, startup, tenants
from rental_tracker.services.common import LockedPeriodError, ServiceError, set_setting
from tests.conftest import make_lease, make_property

TODAY = date(2026, 3, 10)


def count(conn, table, where="1", params=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def audited(conn, entity):
    return count(conn, "audit_log", "action = 'delete' AND entity_type = ?", (entity,))


def test_delete_payment_and_linked_deposit(conn, owner_id):
    lid = make_lease(conn, owner_id)
    pid = ledger.record_payment(conn, lid, 5000, "2026-01-02", "cash")
    deletion.delete_payment(conn, pid)
    assert count(conn, "payments") == 0 and audited(conn, "payment") == 1
    # a deposit applied to rent by an older version: the deposit entry goes with its payment
    pid = conn.execute("INSERT INTO payments(lease_id, received_date, amount_cents, method) "
                       "VALUES (?, '2026-02-01', 40000, 'deposit_applied')", (lid,)).lastrowid
    conn.execute("INSERT INTO deposit_transactions(lease_id, txn_date, txn_type, amount_cents, payment_id) "
                 "VALUES (?, '2026-02-01', 'applied_to_balance', 40000, ?)", (lid, pid))
    assert "matching security deposit" in deletion.delete_payment(conn, pid)
    assert count(conn, "deposit_transactions") == 0


def test_deleted_lines_stay_deleted(conn, owner_id):
    lid = make_lease(conn, owner_id, rent=100000, late_fee_type="flat", late_fee_flat_cents=5000)
    rent_posting.post_rent(conn, TODAY)
    march = conn.execute("SELECT id FROM charges WHERE period = '2026-03' AND charge_type = 'rent'").fetchone()[0]
    ledger.pay_line(conn, lid, march, 20000, TODAY)  # a payment made toward March
    before = ledger.lease_balance(conn, lid)
    assert deletion.delete_charge(conn, march) == "Deleted."
    rent_posting.post_rent(conn, TODAY)  # the latest month is not billed again...
    startup.run_catch_up(conn, TODAY)    # ...not even when the app starts
    assert count(conn, "charges", "period = '2026-03' AND charge_type = 'rent' AND voided_at IS NULL") == 0
    assert ledger.lease_balance(conn, lid) == before - 100000
    assert all(e["id"] != march for e in ledger.ledger_entries(conn, lid) if e["kind"] == "charge")
    assert ledger.lease_summary(conn, lid, TODAY)["balance"] == before - 100000  # the payment still counts
    # a late fee you delete isn't suggested again
    late_fees.apply(conn, [c.key for c in late_fees.find_candidates(conn, TODAY)][:1], "approve", TODAY)
    fee = conn.execute("SELECT id, period FROM charges WHERE charge_type = 'late_fee' AND voided_at IS NULL").fetchone()
    deletion.delete_charge(conn, fee["id"])
    assert fee["period"] not in {c.period for c in late_fees.find_candidates(conn, TODAY)}
    # a debt you added yourself is simply removed
    debt = ledger.add_charge(conn, lid, "other", 500, None, "Debt")
    deletion.delete_charge(conn, debt)
    assert count(conn, "charges", "id = ?", (debt,)) == 0 and audited(conn, "charge") == 3


def test_books_lock_blocks_deletes(conn, owner_id):
    lid = make_lease(conn, owner_id)
    pid = ledger.record_payment(conn, lid, 5000, "2026-01-02", "cash")
    set_setting(conn, "books_locked_through", "2026-01-31")
    with pytest.raises(LockedPeriodError):
        deletion.delete_payment(conn, pid)
    assert "locked" in deletion.preview(conn, "lease", lid).blockers[0]
    with pytest.raises(ServiceError):
        deletion.delete(conn, "lease", lid)


def test_delete_tenancy_removes_history_and_person(conn, owner_id):
    lid = make_lease(conn, owner_id)
    rent_posting.post_rent(conn, TODAY)
    ledger.record_payment(conn, lid, 100000, "2026-01-02", "check")
    conn.execute("INSERT INTO deposit_transactions(lease_id, txn_date, txn_type, amount_cents) "
                 "VALUES (?, '2026-01-01', 'received', 100000)", (lid,))  # from an older version
    assert deletion.preview(conn, "lease", lid).label.startswith("Ann Lee-P-1 at ")
    deletion.delete(conn, "lease", lid)
    for table in ("leases", "charges", "payments", "deposit_transactions", "lease_tenants", "tenants"):
        assert count(conn, table) == 0, table
    assert audited(conn, "lease") == 1


def test_delete_property(conn, owner_id):
    lid = make_lease(conn, owner_id, code="P-1")
    pid = conn.execute("SELECT id FROM properties WHERE code = 'P-1'").fetchone()[0]
    make_property(conn, owner_id, code="P-2")
    assert deletion.preview(conn, "property", pid).label == "Property P-1"
    deletion.delete(conn, "property", pid)
    assert count(conn, "properties") == 1 and count(conn, "leases") == 0 and count(conn, "tenants") == 0
    assert not ledger.load_ledgers(conn, [lid])


def test_only_units_tenancies_and_tenants_can_be_deleted(conn, owner_id):
    make_property(conn, owner_id)
    unit = conn.execute("SELECT id FROM units").fetchone()[0]
    with pytest.raises(ServiceError):  # a unit is deleted through its property
        deletion.delete(conn, "unit", unit)


def test_delete_tenant_rules(conn, owner_id):
    lid = make_lease(conn, owner_id)
    solo = conn.execute("SELECT tenant_id FROM lease_tenants").fetchone()[0]
    assert deletion.preview(conn, "tenant", solo).blockers
    co = tenants.save_tenant(conn, None, {"first_name": "Bo", "last_name": "Kim"})
    conn.execute("INSERT INTO lease_tenants(lease_id, tenant_id, role) VALUES (?, ?, 'co_tenant')", (lid, co))
    deletion.delete(conn, "tenant", solo)
    assert tuple(conn.execute("SELECT tenant_id, role FROM lease_tenants").fetchone()) == (co, "primary")
    loner = tenants.save_tenant(conn, None, {"first_name": "No", "last_name": "Lease"})
    deletion.delete(conn, "tenant", loner)
    assert count(conn, "tenants") == 1
