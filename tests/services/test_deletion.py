from datetime import date

import pytest

from rental_tracker.services import deletion, leases, ledger, rent_posting, tenants
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


def test_delete_auto_rent_rebills_at_current_rent(conn, owner_id):
    lid = make_lease(conn, owner_id, rent=100000)
    rent_posting.post_rent(conn, TODAY)
    leases.add_rent_change(conn, lid, "2026-03-01", 110000)
    march = conn.execute("SELECT id FROM charges WHERE period = '2026-03'").fetchone()[0]
    assert "billed again" in deletion.delete_charge(conn, march, TODAY)
    assert conn.execute("SELECT amount_cents FROM charges WHERE period = '2026-03'").fetchone()[0] == 110000
    debt = ledger.add_charge(conn, lid, "other", 500, None, "Debt")
    assert deletion.delete_charge(conn, debt, TODAY) == "Charge deleted."


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
