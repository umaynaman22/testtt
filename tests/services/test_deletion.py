from datetime import date

import pytest

from rental_tracker.services import deletion, leases, ledger, portfolio, rent_posting, tenants
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
    ledger.record_deposit(conn, lid, "received", 100000, "2026-01-01")
    ledger.record_deposit(conn, lid, "applied_to_balance", 40000, "2026-02-01")
    applied = conn.execute("SELECT payment_id FROM deposit_transactions WHERE payment_id IS NOT NULL").fetchone()[0]
    assert "matching security deposit" in deletion.delete_payment(conn, applied)
    assert ledger.deposit_held(conn, lid) == 100000


def test_delete_auto_rent_rebills_at_current_rent(conn, owner_id):
    lid = make_lease(conn, owner_id, rent=100000)
    rent_posting.post_rent(conn, TODAY)
    leases.add_rent_change(conn, lid, "2026-03-01", 110000)
    march = conn.execute("SELECT id FROM charges WHERE period = '2026-03'").fetchone()[0]
    assert "billed again" in deletion.delete_charge(conn, march, TODAY)
    assert conn.execute("SELECT amount_cents FROM charges WHERE period = '2026-03'").fetchone()[0] == 110000
    credit = ledger.add_credit(conn, lid, 500, None, None)
    assert deletion.delete_charge(conn, credit, TODAY) == "Credit deleted."


def test_deposit_delete_guard(conn, owner_id):
    lid = make_lease(conn, owner_id)
    got = ledger.record_deposit(conn, lid, "received", 100000, "2026-01-01")
    refund = ledger.record_deposit(conn, lid, "refund", 60000, "2026-02-01")
    with pytest.raises(ServiceError, match="Delete those entries first"):
        deletion.delete_deposit(conn, got)
    deletion.delete_deposit(conn, refund)
    deletion.delete_deposit(conn, got)
    assert ledger.deposit_held(conn, lid) == 0


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
    ledger.record_deposit(conn, lid, "received", 100000, "2026-01-01")
    p = deletion.preview(conn, "lease", lid)
    assert any("payment" in r for r in p.removes) and any("tenant record" in r for r in p.removes)
    deletion.delete(conn, "lease", lid)
    for table in ("leases", "charges", "payments", "deposit_transactions", "lease_tenants", "tenants"):
        assert count(conn, table) == 0, table
    assert audited(conn, "lease") == 1


def test_delete_property(conn, owner_id):
    lid = make_lease(conn, owner_id, code="P-1")
    pid = conn.execute("SELECT id FROM properties WHERE code = 'P-1'").fetchone()[0]
    make_property(conn, owner_id, code="P-2")
    p = deletion.preview(conn, "property", pid)
    assert "1 unit" in p.removes and "1 tenant record" in p.removes
    deletion.delete(conn, "property", pid)
    assert count(conn, "properties") == 1 and count(conn, "leases") == 0 and count(conn, "tenants") == 0
    assert not ledger.load_ledgers(conn, [lid])


def test_delete_unit(conn, owner_id):
    make_property(conn, owner_id, units=["A", "B"])
    unit_a = conn.execute("SELECT id FROM units WHERE unit_label = 'A'").fetchone()[0]
    deletion.delete(conn, "unit", unit_a)
    assert count(conn, "units") == 1


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


def test_delete_add_on_and_rent_change(conn, owner_id):
    lid = make_lease(conn, owner_id)
    rc = leases.add_recurring_charge(conn, lid, charge_type="pet_rent", description="Dog", amount_cents=3000,
                                     start="2026-01-01")
    rent_posting.post_rent(conn, TODAY)
    assert "3 months already billed" in deletion.delete_recurring_charge(conn, rc)
    rent_posting.post_rent(conn, date(2026, 4, 1))
    assert count(conn, "charges", "charge_type = 'pet_rent'") == 3
    change = leases.add_rent_change(conn, lid, "2026-06-01", 120000)
    deletion.delete_rent_change(conn, change)
    assert count(conn, "lease_rent_changes") == 0


def test_property_helpers(conn):
    assert portfolio.parse_unit_labels("") is None
    assert portfolio.parse_unit_labels("A, B, A") == ["A", "B"]
