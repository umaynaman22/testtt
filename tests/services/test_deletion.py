from datetime import date

import pytest

from rental_tracker.services import deletion, expenses, leases, ledger, portfolio, rent_posting, tenants
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
    applied = conn.execute("SELECT payment_id FROM payments p JOIN deposit_transactions d ON d.payment_id = p.id").fetchone()[0]
    assert "matching security deposit" in deletion.delete_payment(conn, applied)
    assert ledger.deposit_held(conn, lid) == 100000


def test_delete_auto_rent_rebills_at_current_rent(conn, owner_id):
    lid = make_lease(conn, owner_id, rent=100000)
    rent_posting.post_rent(conn, TODAY)
    leases.add_rent_change(conn, lid, "2026-03-01", 110000)
    march = conn.execute("SELECT id FROM charges WHERE period = '2026-03'").fetchone()[0]
    msg = deletion.delete_charge(conn, march, TODAY)
    assert "billed again" in msg
    assert conn.execute("SELECT amount_cents FROM charges WHERE period = '2026-03'").fetchone()[0] == 110000
    credit = ledger.add_credit(conn, lid, 500, "2026-03-02", "goodwill")
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
    p = deletion.preview(conn, "lease", lid)
    assert p.blockers and "locked" in p.blockers[0]
    with pytest.raises(ServiceError):
        deletion.delete(conn, "lease", lid, typed="DELETE")


def test_delete_lease_cascades_and_needs_typed_confirm(conn, owner_id):
    lid = make_lease(conn, owner_id)
    rent_posting.post_rent(conn, TODAY)
    ledger.record_payment(conn, lid, 100000, "2026-01-02", "check")
    ledger.record_deposit(conn, lid, "received", 100000, "2026-01-01")
    leases.add_recurring_charge(conn, lid, charge_type="parking", description="Parking", amount_cents=5000,
                                start="2026-01-01")
    p = deletion.preview(conn, "lease", lid)
    assert p.needs_typed_confirm and any("payment" in r for r in p.removes)
    with pytest.raises(ServiceError, match="Type DELETE"):
        deletion.delete(conn, "lease", lid, typed="yes")
    deletion.delete(conn, "lease", lid, typed="delete")
    for table in ("leases", "charges", "payments", "deposit_transactions", "lease_tenants", "lease_recurring_charges"):
        assert count(conn, table) == 0, table
    assert count(conn, "tenants") == 1 and audited(conn, "lease") == 1


def test_delete_property_and_owner(conn, owner_id):
    lid = make_lease(conn, owner_id, code="P-1")
    pid = conn.execute("SELECT id FROM properties WHERE code = 'P-1'").fetchone()[0]
    cat = conn.execute("SELECT id FROM expense_categories LIMIT 1").fetchone()[0]
    expenses.create_expense(conn, category_id=cat, expense_date="2026-01-05", amount_cents=100, property_id=pid)
    make_property(conn, owner_id, code="P-2")
    portfolio.set_property_tags(conn, pid, ["Only here"])
    p = deletion.preview(conn, "property", pid)
    assert "1 expense" in p.removes and "1 unit" in p.removes and p.warnings
    deletion.delete(conn, "property", pid, typed="DELETE")
    assert count(conn, "properties") == 1 and count(conn, "expenses") == 0 and count(conn, "leases") == 0
    assert count(conn, "tags") == 0
    assert not ledger.load_ledgers(conn, [lid])
    p = deletion.preview(conn, "owner", owner_id)
    assert "1 property" in p.removes
    deletion.delete(conn, "owner", owner_id, typed="DELETE")
    assert count(conn, "owners") == 0 and count(conn, "properties") == 0 and count(conn, "units") == 0


def test_delete_unit_keeps_expenses(conn, owner_id):
    pid = make_property(conn, owner_id, units=["A", "B"])
    unit_a = conn.execute("SELECT id FROM units WHERE unit_label = 'A'").fetchone()[0]
    cat = conn.execute("SELECT id FROM expense_categories LIMIT 1").fetchone()[0]
    expenses.create_expense(conn, category_id=cat, expense_date="2026-01-05", amount_cents=100, unit_id=unit_a)
    p = deletion.preview(conn, "unit", unit_a)
    assert not p.needs_typed_confirm and "stay on the property" in p.keeps[0]
    deletion.delete(conn, "unit", unit_a)
    assert count(conn, "units") == 1
    assert tuple(conn.execute("SELECT property_id, unit_id FROM expenses").fetchone()) == (pid, None)


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


def test_delete_vendor_and_category(conn, owner_id):
    vid = expenses.find_or_create_vendor(conn, "Ace")
    cats = [r[0] for r in conn.execute("SELECT id FROM expense_categories ORDER BY id LIMIT 2")]
    eid = expenses.create_expense(conn, category_id=cats[0], expense_date="2026-01-05", amount_cents=100, vendor_id=vid)
    deletion.delete(conn, "vendor", vid)
    assert conn.execute("SELECT vendor_id FROM expenses").fetchone()[0] is None
    p = deletion.preview(conn, "category", cats[0])
    assert p.move_choices
    with pytest.raises(ServiceError, match="Choose where"):
        deletion.delete(conn, "category", cats[0])
    deletion.delete(conn, "category", cats[0], move_to=cats[1])
    assert conn.execute("SELECT category_id FROM expenses WHERE id = ?", (eid,)).fetchone()[0] == cats[1]
    deletion.delete_expense(conn, eid)
    assert count(conn, "expenses") == 0


def test_delete_add_on_keeps_billed_months(conn, owner_id):
    lid = make_lease(conn, owner_id)
    rc = leases.add_recurring_charge(conn, lid, charge_type="pet_rent", description="Dog", amount_cents=3000,
                                     start="2026-01-01")
    rent_posting.post_rent(conn, TODAY)
    assert "3 months already billed" in deletion.delete_recurring_charge(conn, rc)
    assert count(conn, "charges", "charge_type = 'pet_rent'") == 3
    rent_posting.post_rent(conn, date(2026, 4, 1))
    assert count(conn, "charges", "charge_type = 'pet_rent'") == 3  # not billed any more
    change = leases.add_rent_change(conn, lid, "2026-06-01", 120000)
    deletion.delete_rent_change(conn, change)
    assert count(conn, "lease_rent_changes") == 0
