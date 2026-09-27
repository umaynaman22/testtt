from datetime import date

from rental_tracker.services import dashboard, leases, ledger, portfolio, reports, rent_posting, tenants
from tests.conftest import make_lease

TODAY = date(2026, 3, 20)


def test_optional_fields_everywhere(conn):
    pid = portfolio.save_property(conn, None, {})
    prop = portfolio.get_property(conn, pid)
    assert prop["name"] == "Unit 1" and prop["code"] == "Unit 1"
    pid2 = portfolio.save_property(conn, None, {"name": "12 Maple St"}, unit_labels=["1", "2", "3"])
    assert [r[0] for r in conn.execute("SELECT unit_label FROM units WHERE property_id = ? ORDER BY id", (pid2,))] \
        == ["1", "2", "3"]
    assert portfolio.save_property(conn, None, {"name": "12 Maple St"}) != pid2  # same name is fine
    assert portfolio.get_property(conn, pid2)["code"] == "12 Maple St"
    tid = tenants.save_tenant(conn, None, {})
    assert tenants.get_tenant(conn, tid)["first_name"] == "Unnamed"
    assert tenants.split_name("Ann") == ("Ann", "") and tenants.split_name("Lee, Ann") == ("Ann", "Lee")


def test_flatten_units_makes_one_unit_per_property(conn, owner_id):
    """The app lists units, not properties; older multi-unit properties are split up."""
    building = portfolio.save_property(conn, None, {"name": "251 Osmena St", "city": "Cebu City"},
                                       unit_labels=["1", "2", "3"])
    units = dict(conn.execute("SELECT unit_label, id FROM units WHERE property_id = ?", (building,)).fetchall())
    tid = tenants.save_tenant(conn, None, {"first_name": "Ana"})
    lid = leases.create_lease(conn, unit_id=units["2"], tenants=[(tid, "primary")], start="2026-01-01", end=None,
                              rent_cents=900000, today=TODAY)
    house = portfolio.save_property(conn, None, {"name": "9 Luna St"})
    extra = portfolio.save_unit(conn, None, house, {"unit_label": "Garage"})
    single = portfolio.save_property(conn, None, {"name": "Sunrise"}, unit_labels=["2B"])
    empty = portfolio.save_property(conn, None, {"name": "Lot"})
    conn.execute("DELETE FROM units WHERE property_id = ?", (empty,))
    balance = ledger.lease_summary(conn, lid, TODAY)["balance"]

    assert portfolio.flatten_units(conn) == 3 + 1 + 1 + 1
    names = {r["name"] for r in conn.execute("SELECT name FROM properties")}
    assert {"251 Osmena St · 1", "251 Osmena St · 2", "251 Osmena St · 3", "9 Luna St", "9 Luna St · Garage",
            "Sunrise · 2B", "Lot"} <= names
    assert conn.execute("SELECT COUNT(*) FROM units WHERE unit_label <> 'Main'").fetchone()[0] == 0
    assert conn.execute("SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM units GROUP BY property_id)").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM properties p WHERE NOT EXISTS "
                        "(SELECT 1 FROM units u WHERE u.property_id = p.id)").fetchone()[0] == 0
    moved = portfolio.get_unit(conn, units["2"])
    assert moved["property_name"] == "251 Osmena St · 2"
    assert portfolio.get_property(conn, moved["property_id"])["city"] == "Cebu City"  # address copied over
    assert leases.get_lease(conn, lid)["unit_id"] == units["2"]
    assert ledger.lease_summary(conn, lid, TODAY)["balance"] == balance  # money untouched
    assert portfolio.get_unit(conn, extra)["property_name"] == "9 Luna St · Garage"
    assert portfolio.get_property(conn, single)["name"] == "Sunrise · 2B"
    assert portfolio.flatten_units(conn) == 0  # nothing left to do


def test_tenancies_show_late_tenants(conn, owner_id):
    on_time = make_lease(conn, owner_id, code="A", late_fee_type="flat", late_fee_flat_cents=5000)
    behind = make_lease(conn, owner_id, code="B")
    rent_posting.post_rent(conn, TODAY)
    ledger.record_payment(conn, on_time, 300000, "2026-03-01", None)  # no method given
    rows = {r["lease_id"]: r for r in tenants.tenancies(conn, today=TODAY)}
    assert rows[on_time]["state"] == "paid" and rows[on_time]["last_paid_on"] == "2026-03-01"
    assert rows[behind]["state"] == "late" and rows[behind]["past_due_cents"] == 300000
    assert rows[behind]["days_late"] == (TODAY - date(2026, 1, 1)).days
    loner = tenants.save_tenant(conn, None, {"first_name": "No", "last_name": "Home"})
    assert any(r["tenant_id"] == loner and r["state"] == "none" for r in tenants.tenancies(conn, today=TODAY))
    assert [r["lease_id"] for r in tenants.tenancies(conn, today=TODAY, q="lee-b")] == [behind]


def test_reports_and_dashboard(conn, owner_id):
    lid = make_lease(conn, owner_id, code="A")
    make_lease(conn, owner_id, code="B")
    portfolio.save_property(conn, None, {"name": "Empty house"})
    rent_posting.post_rent(conn, TODAY)
    ledger.record_payment(conn, lid, 250000, "2026-03-02", "check")
    rr = reports.rent_roll(conn)
    assert [r["property_code"] for r in rr.rows] == ["A", "B"]  # empty units aren't listed
    assert rr.totals["balance_cents"] == 50000 + 300000
    assert {r["property_code"] for r in reports.aging_report(conn, TODAY).rows} == {"A", "B"}
    coll = reports.collections(conn, "2026-03")
    assert coll.totals["billed_cents"] == 200000 and coll.totals["paid_cents"] == 50000
    assert "A" in coll.to_csv()
    d = dashboard.build(conn, TODAY)
    assert len(d["late"]) == 2 and d["owed"] == 350000 and "vacant" not in d
