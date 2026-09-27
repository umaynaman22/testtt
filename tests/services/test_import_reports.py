from datetime import date

from rental_tracker.services import dashboard, expenses, importer, ledger, reports, rent_posting, search

TODAY = date(2026, 10, 5)

FILES = {
    "owners": "name,entity_type\nAcme Rentals LLC,llc\n",
    "properties": ("code,owner_name,name,property_type,address_line1,city,state,postal_code,tags,estimated_value\n"
                   "oak-1,Acme Rentals LLC,1 Oak St,single_family,1 Oak St,Springfield,IL,62701,North; Section 8,200000\n"
                   "DUP-2,Acme Rentals LLC,2 Elm Duplex,multi_family,2 Elm St,Springfield,IL,62702,North,\n"),
    "units": "property_code,unit_label,market_rent\nDUP-2,A,1200\nDUP-2,B,1250\n",
    "tenants": "tenant_key,first_name,last_name,phone\nT1,Ann,Lee,555-0101\nT2,Bo,Kim,\nT3,José,Núñez,\n",
    "vendors": "name,trade,needs_1099\nAce Plumbing,plumber,yes\n",
    "leases": ("property_code,unit_label,tenant_keys,start_date,end_date,rent,deposit,late_fee_type,late_fee_amount\n"
               "OAK-1,,T1;T2,2021-03-01,2022-02-28,1500,1500,flat,50\n"
               "DUP-2,A,T3,2025-11-01,2026-10-31,1150,1150,percent,5\n"),
    "opening_balances": "property_code,unit_label,balance,deposit_held,as_of_date\nOAK-1,,300.00,1500,2026-09-30\nDUP-2,A,0,1150,2026-09-30\n",
}


def test_dry_run_saves_nothing_then_commit(conn):
    dry = importer.run_import(conn, FILES, commit=False, today=TODAY)
    assert dry.errors == [] and not dry.committed
    assert dry.counts["leases"]["created"] == 2
    assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 0
    real = importer.run_import(conn, FILES, commit=True, today=TODAY)
    assert real.committed and real.errors == []
    oak = conn.execute("SELECT * FROM leases l JOIN units u ON u.id = l.unit_id "
                       "JOIN properties p ON p.id = u.property_id WHERE p.code = 'OAK-1'").fetchone()
    assert oak["status"] == "month_to_month" and oak["billing_start_date"] == "2026-10-01"
    assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 3  # OAK-1 got 'Main'
    rent_posting.post_rent(conn, TODAY)
    # opening balance + October rent only — no rent from 2021
    assert ledger.lease_balance(conn, oak["id"]) == 30000 + 150000
    assert [r["title"] for r in search.search(conn, "jose")] == ["José Núñez"]
    again = importer.run_import(conn, FILES, commit=False, today=TODAY)
    assert again.counts["leases"]["skipped"] == 2
    assert any("already has an opening balance" in e.message for e in again.errors)


def test_errors_reported_per_row(conn):
    files = dict(FILES)
    files["leases"] = ("property_code,unit_label,tenant_keys,start_date,rent\n"
                       "NOPE,,T1,2026-01-01,100\nDUP-2,,T1,2026-01-01,100\nOAK-1,,T9,2026-01-01,abc\n")
    res = importer.run_import(conn, files, commit=True, today=TODAY)
    assert not res.committed
    msgs = {(e.row, e.message) for e in res.errors if e.file == "leases"}
    assert (2, "unknown property_code 'NOPE'") in msgs
    assert any(r == 3 and "fill in unit_label" in m for r, m in msgs)
    assert any(r == 4 and "unknown tenant_key 'T9'" in m for r, m in msgs)
    assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 0


def test_missing_columns(conn):
    res = importer.run_import(conn, {"tenants": "first_name,last_name\nA,B\n"}, commit=True, today=TODAY)
    assert res.errors[0].message == "missing column(s): tenant_key"


def test_templates_parse():
    for kind in importer.KINDS:
        assert importer.template_csv(kind).count("\n") == 2


def test_reports_and_dashboard(conn, data_dir):
    importer.run_import(conn, FILES, commit=True, today=TODAY)
    rent_posting.post_rent(conn, TODAY)
    oak = conn.execute("SELECT l.id FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p "
                       "ON p.id = u.property_id WHERE p.code = 'OAK-1'").fetchone()[0]
    ledger.record_payment(conn, oak, 100000, "2026-10-03", "check")
    cat = conn.execute("SELECT id FROM expense_categories WHERE name = 'Repairs'").fetchone()[0]
    vendor = expenses.find_or_create_vendor(conn, "Ace Plumbing")
    pid = conn.execute("SELECT id FROM properties WHERE code = 'OAK-1'").fetchone()[0]
    expenses.create_expense(conn, category_id=cat, expense_date="2026-10-04", amount_cents=25000,
                            property_id=pid, vendor_id=vendor)
    rr = reports.rent_roll(conn)
    assert len(rr.rows) == 3 and rr.totals["balance_cents"] == 30000 + 150000 - 100000 + 115000
    ag = reports.aging_report(conn, TODAY)
    assert {r["property_code"] for r in ag.rows} == {"OAK-1", "DUP-2"}
    pl = reports.income_statement(conn, "2026-10-01", "2026-10-31")
    amounts = {r["line"]: r.get("amount") for r in pl.rows}
    assert amounts["Total income"] == 100000 and amounts["Total operating expenses"] == 25000
    assert amounts["Net operating income (NOI)"] == 75000
    se = reports.schedule_e(conn, 2026)
    assert se.totals["rents"] == 100000 and se.totals["Sch E line 14"] == 25000
    assert "OAK-1" in reports.collections(conn, "2026-10").to_csv()
    assert len(reports.vacancy(conn, TODAY).rows) == 1
    assert reports.deposit_register(conn, TODAY).totals["held_cents"] == 265000
    assert reports.vendor_1099(conn, 2026, threshold_cents=100).rows[0]["name"] == "Ace Plumbing"
    perf = reports.property_performance(conn, "2026-01-01", "2026-12-31")
    assert {r["property_code"]: r["noi"] for r in perf.rows}["OAK-1"] == 75000
    d = dashboard.build(conn, TODAY, data_dir)
    assert d["units"] == 3 and d["occupied"] == 2 and d["received"] == 100000
    assert d["collected"] == 70000  # the payment covered the $300 opening balance first
    assert d["delinquent"] == 2
