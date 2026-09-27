from datetime import date

from rental_tracker.services import dashboard, ledger, portfolio, reports, rent_posting, tenants
from tests.conftest import make_lease

TODAY = date(2026, 3, 20)


def test_optional_fields_everywhere(conn):
    pid = portfolio.save_property(conn, None, {})
    prop = portfolio.get_property(conn, pid)
    assert prop["name"] == "Property 1" and prop["code"] == "Property 1"
    pid2 = portfolio.save_property(conn, None, {"name": "12 Maple St"}, unit_labels=portfolio.parse_unit_labels("3"))
    assert [u["unit_label"] for u in portfolio.units_for_property(conn, pid2)] == ["1", "2", "3"]
    assert portfolio.save_property(conn, None, {"name": "12 Maple St"}) != pid2  # same name is fine
    assert portfolio.get_property(conn, pid2)["code"] == "12 Maple St"
    tid = tenants.save_tenant(conn, None, {})
    assert tenants.get_tenant(conn, tid)["first_name"] == "Unnamed"
    assert tenants.split_name("Ann") == ("Ann", "") and tenants.split_name("Lee, Ann") == ("Ann", "Lee")


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
    assert len(rr.rows) == 3 and rr.totals["balance_cents"] == 50000 + 300000
    assert {r["property_code"] for r in reports.aging_report(conn, TODAY).rows} == {"A", "B"}
    coll = reports.collections(conn, "2026-03")
    assert coll.totals["billed_cents"] == 200000 and coll.totals["paid_cents"] == 50000
    assert "A" in coll.to_csv()
    assert [r["property_code"] for r in reports.vacancy(conn, TODAY).rows] == ["Empty house"]
    d = dashboard.build(conn, TODAY)
    assert d["units"] == 3 and d["occupied"] == 2 and len(d["late"]) == 2 and d["owed"] == 350000
