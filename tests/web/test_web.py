import re
import time
from datetime import date
from pathlib import Path

import pytest

from rental_tracker import db as dbmod
from rental_tracker.demo import build_demo
from rental_tracker.web import create_app

TODAY = date(2026, 9, 27)
TOKEN = "test-token"


@pytest.fixture
def app(data_dir):
    build_demo(data_dir, today=TODAY)
    return create_app(data_dir, launch_token=TOKEN, today_fn=lambda: TODAY, testing=True)


@pytest.fixture
def client(app):
    c = app.test_client()
    assert c.get(f"/auth?token={TOKEN}").status_code == 302
    return c


def csrf(client) -> str:
    html = client.get("/").get_data(as_text=True)
    return re.search(r'"X-CSRF-Token": "([^"]+)"', html).group(1)


def q(app, sql):
    conn = dbmod.connect(app.extensions["rental_tracker"].data.db)
    try:
        return [r[0] for r in conn.execute(sql)]
    finally:
        conn.close()


def test_requires_launch_token(app):
    c = app.test_client()
    assert c.get("/").status_code == 403
    assert c.get("/auth?token=wrong").status_code == 403
    assert c.get(f"/auth?token={TOKEN}").status_code == 302
    assert c.get("/").status_code == 200


def test_rejects_foreign_host(client):
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:5000"}).status_code != 400  # allowed host


def test_post_requires_csrf(client):
    assert client.post("/properties/new", data={"name": "x"}).status_code == 400
    assert client.post("/properties/new", data={"name": "x", "csrf_token": csrf(client)}).status_code == 302


def test_every_page_renders(app, client):
    pid, unit, lease, tid, pay = (q(app, f"SELECT id FROM {t} LIMIT 1")[0]
                                  for t in ("properties", "units", "leases", "tenants", "payments"))
    pages = ["/", "/properties", "/properties?sort=balance", "/properties/new", f"/properties/{pid}",
             f"/properties/{pid}/edit", f"/units/{unit}", "/tenants", "/tenants?q=lee",
             "/tenants?format=csv", "/tenants/new", f"/tenants/new?unit_id={unit}", f"/tenants/{tid}",
             f"/tenants/{tid}/edit", "/rent-day", "/rent-day?period=2026-08&show=unpaid", "/late", "/payments",
             "/payments?format=csv", f"/payments/{pay}/receipt", "/reports", "/settings", "/search?q=rizal",
             "/search?q=zzzz", f"/delete/property/{pid}", f"/delete/unit/{unit}", f"/delete/lease/{lease}",
             f"/delete/tenant/{tid}"]
    for key in ("rent-roll", "aging", "collections"):
        pages += [f"/reports/{key}", f"/reports/{key}?format=csv", f"/reports/{key}?property={pid}"]
    for lid in q(app, "SELECT id FROM leases"):
        pages += [f"/leases/{lid}", f"/leases/{lid}/edit", f"/leases/{lid}/statement"]
    for page in pages:
        resp = client.get(page)
        assert resp.status_code in (200, 302), f"{page} -> {resp.status_code}"
    assert client.get("/properties/999999").status_code == 404
    assert client.get("/leases/999999").status_code == 404
    assert client.get("/reports/deposits").status_code == 404
    assert client.get("/reports/vacancy").status_code == 404
    for gone in ("/expenses", "/vendors", "/owners", "/import", "/backups", "/audit", "/leases"):
        assert client.get(gone).status_code == 404, gone


def test_no_required_fields_anywhere():
    root = Path(__file__).resolve().parents[2] / "src" / "rental_tracker" / "web" / "templates"
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<(input|select|textarea)[^>]*\srequired\b", text), path
        assert "<em>*</em>" not in text, path


def test_no_external_urls_in_templates_or_static():
    root = Path(__file__).resolve().parents[2] / "src" / "rental_tracker" / "web"
    for path in list((root / "templates").rglob("*.html")) + [root / "static" / "app.css", root / "static" / "app.js"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(src|href)=[\"']https?://", text), path


def test_simple_workflow(app, client):
    token = csrf(client)
    # a property with nothing filled in
    r = client.post("/properties/new", data={"csrf_token": token})
    assert r.status_code == 302
    pid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    unit = q(app, f"SELECT id FROM units WHERE property_id = {pid}")[0]
    assert "Add tenant" in client.get(f"/properties/{pid}").get_data(as_text=True)
    # a tenant who has lived there since last year: rent is billed from the move-in date, plus a debt
    r = client.post("/tenants/new", data={"csrf_token": token, "unit_id": unit, "name": "Rita Moreno",
                                          "rent": "1,200", "moved_in": "2025-05-01", "debt": "300"})
    assert r.status_code == 302, r.get_data(as_text=True)[:2000]
    lid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = client.get(f"/leases/{lid}").get_data(as_text=True)
    assert "Rita Moreno" in page and ">Debt<" in page and "Fill in past rent" in page
    assert q(app, f"SELECT COUNT(*) FROM charges WHERE lease_id = {lid} AND charge_type = 'rent'")[0] == 17  # May 2025-Sep 2026
    assert q(app, f"SELECT balance_cents FROM v_lease_balances WHERE lease_id = {lid}")[0] == 17 * 120000 + 30000
    # they had paid all that rent: fill in the payment history up to today
    r = client.post(f"/leases/{lid}/fill-rent", data={"csrf_token": token, "method": "cash"}, follow_redirects=True)
    assert "Marked 17 rent bills as paid" in r.get_data(as_text=True)
    assert q(app, f"SELECT COUNT(*) FROM payments WHERE lease_id = {lid} AND method = 'cash'")[0] == 17
    assert q(app, f"SELECT MIN(received_date) FROM payments WHERE lease_id = {lid}") == ["2025-05-01"]
    assert q(app, f"SELECT balance_cents FROM v_lease_balances WHERE lease_id = {lid}")[0] == 30000  # the debt is left
    assert "Fill in past rent" not in client.get(f"/leases/{lid}").get_data(as_text=True)
    # a payment with only an amount
    client.post(f"/leases/{lid}/payment", data={"csrf_token": token, "amount": "300"})
    assert q(app, f"SELECT balance_cents FROM v_lease_balances WHERE lease_id = {lid}")[0] == 0
    assert q(app, f"SELECT method FROM payments WHERE lease_id = {lid} ORDER BY id DESC LIMIT 1")[0] == "other"
    # Collect rent quick entry, and the typo guard
    r = client.post("/rent-day/pay", data={"lease_id": lid, "amount": "310000", "period": "2026-09"},
                    headers={"HX-Request": "true", "X-CSRF-Token": token})
    assert "far more than this lease owes" in r.get_data(as_text=True)
    # edit: change rent after billing -> starts with the next bill
    client.post(f"/leases/{lid}/edit", data={"csrf_token": token, "name": "Rita M", "rent": "1300"})
    assert q(app, f"SELECT rent_cents FROM lease_rent_changes WHERE lease_id = {lid}") == [130000]
    assert q(app, "SELECT first_name FROM tenants WHERE last_name = 'M'") == ["Rita"]
    # add a debt with a note, then one with only an amount
    client.post(f"/leases/{lid}/debt", data={"csrf_token": token, "amount": "75", "note": "broken window"})
    client.post(f"/leases/{lid}/debt", data={"csrf_token": token, "amount": "25"})
    assert q(app, f"SELECT description FROM charges WHERE lease_id = {lid} AND charge_type = 'other' ORDER BY id") == \
        ["Debt — broken window", "Debt"]
    assert q(app, f"SELECT balance_cents FROM v_lease_balances WHERE lease_id = {lid}")[0] == 10000
    assert "Debt — broken window" in client.get(f"/leases/{lid}").get_data(as_text=True)
    client.post(f"/leases/{lid}/debt", data={"csrf_token": token})  # nothing entered: nothing added
    assert q(app, f"SELECT COUNT(*) FROM charges WHERE lease_id = {lid} AND charge_type = 'other'")[0] == 2


def test_removed_fields_and_actions(app, client):
    lid = q(app, "SELECT id FROM leases WHERE status = 'active' LIMIT 1")[0]
    page = client.get(f"/leases/{lid}").get_data(as_text=True)
    assert "Add debt" in page
    for gone in ("Add a charge", "Give a credit", "Change the rent", "Security deposit", "Deposit held",
                 "Moved out", "mailto:"):
        assert gone not in page, gone
    for url in (f"/leases/{lid}/charge", f"/leases/{lid}/credit", f"/leases/{lid}/deposit",
                f"/leases/{lid}/rent-change", f"/leases/{lid}/move-out"):
        assert client.post(url, data={"csrf_token": csrf(client)}).status_code == 404, url
    forms = client.get("/tenants/new").get_data(as_text=True) + client.get(f"/leases/{lid}/edit").get_data(as_text=True)
    for gone in ('name="email"', 'name="second_name"', 'name="end_date"', 'name="deposit"', "Already owes"):
        assert gone not in forms, gone
    assert 'name="debt"' in forms
    tid = q(app, "SELECT id FROM tenants LIMIT 1")[0]
    assert 'name="email"' not in client.get(f"/tenants/{tid}/edit").get_data(as_text=True)
    assert 'class="tabs"' not in client.get("/tenants").get_data(as_text=True)


def test_fill_in_past_rent_up_to_a_date_and_rebill_on_edit(app, client):
    token = csrf(client)
    pid = client.post("/properties/new", data={"csrf_token": token, "name": "5 Luna St"}).headers["Location"].split("/")[-1]
    unit = q(app, f"SELECT id FROM units WHERE property_id = {pid}")[0]
    lid = client.post("/tenants/new", data={"csrf_token": token, "unit_id": unit, "name": "Ana Cruz", "rent": "10000",
                                            "moved_in": "2026-01-01", "late_fee": "500"}).headers["Location"].split("/")[-1]
    assert q(app, f"SELECT COUNT(*) FROM charges WHERE lease_id = {lid} AND charge_type = 'rent'")[0] == 9  # Jan-Sep
    # paid through June only
    client.post(f"/leases/{lid}/fill-rent", data={"csrf_token": token, "through": "2026-06-30", "method": "other",
                                                  "method_other": "GCash padala"})
    assert q(app, f"SELECT COUNT(*) FROM payments WHERE lease_id = {lid} AND method_other = 'GCash padala'")[0] == 6
    assert q(app, f"SELECT balance_cents FROM v_lease_balances WHERE lease_id = {lid}")[0] == 3 * 1000000
    late = client.get("/late").get_data(as_text=True)
    assert "Ana Cruz" in late  # July to September are still owed
    from rental_tracker.services import late_fees
    conn = dbmod.connect(app.extensions["rental_tracker"].data.db)
    periods = {c.period for c in late_fees.find_candidates(conn, TODAY) if c.lease_id == int(lid)}
    conn.close()
    assert periods == {"2026-07", "2026-08", "2026-09"}  # no late fees for the months filled in as paid
    r = client.post(f"/leases/{lid}/fill-rent", data={"csrf_token": token, "through": "2026-06-30"}, follow_redirects=True)
    assert "no unpaid rent up to that date" in r.get_data(as_text=True)
    # moving the move-in date later re-bills from then; payments stay
    client.post(f"/leases/{lid}/edit", data={"csrf_token": token, "name": "Ana Cruz", "rent": "10000",
                                             "moved_in": "2026-03-01"})
    assert q(app, f"SELECT MIN(period) FROM charges WHERE lease_id = {lid} AND charge_type = 'rent'") == ["2026-03"]
    assert q(app, f"SELECT COUNT(*) FROM payments WHERE lease_id = {lid}")[0] == 6
    assert q(app, f"SELECT balance_cents FROM v_lease_balances WHERE lease_id = {lid}")[0] == 7 * 1000000 - 6 * 1000000


def test_older_tenants_can_be_billed_from_move_in(app, client):
    """Tenants added before rent was billed from the move-in date can get their past months billed."""
    token = csrf(client)
    lid, start, billed_from = dbmod.connect(app.extensions["rental_tracker"].data.db).execute(
        "SELECT id, start_date, billing_start_date FROM leases WHERE billing_start_date > start_date "
        "AND status = 'active' LIMIT 1").fetchone()
    edit = client.get(f"/leases/{lid}/edit").get_data(as_text=True)
    assert 'name="bill_from_move_in"' in edit
    before = q(app, f"SELECT COUNT(*) FROM charges WHERE lease_id = {lid} AND charge_type = 'rent'")[0]
    client.post(f"/leases/{lid}/edit", data={"csrf_token": token, "moved_in": start})  # saving alone changes nothing
    assert q(app, f"SELECT COUNT(*) FROM charges WHERE lease_id = {lid} AND charge_type = 'rent'")[0] == before
    client.post(f"/leases/{lid}/edit", data={"csrf_token": token, "moved_in": start, "bill_from_move_in": "1"})
    assert q(app, f"SELECT MIN(period) FROM charges WHERE lease_id = {lid} AND charge_type = 'rent'") == [start[:7]]
    assert q(app, f"SELECT billing_start_date FROM leases WHERE id = {lid}") == [None]
    assert 'name="bill_from_move_in"' not in client.get(f"/leases/{lid}/edit").get_data(as_text=True)


def test_tenant_without_property_can_be_linked_later(app, client):
    token = csrf(client)
    r = client.post("/tenants/new", data={"csrf_token": token, "name": "Sam Lee"})
    tid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = client.get(f"/tenants/{tid}").get_data(as_text=True)
    assert "Not linked to a unit" in page
    assert "No unit" in client.get("/tenants").get_data(as_text=True)
    pid = client.post("/properties/new", data={"csrf_token": token, "name": "9 Oak Ct"}).headers["Location"].split("/")[-1]
    unit = q(app, f"SELECT id FROM units WHERE property_id = {pid}")[0]
    r = client.post("/tenants/new", data={"csrf_token": token, "tenant_id": tid, "unit_id": unit, "rent": "900"})
    assert "/leases/" in r.headers["Location"]
    assert client.get(f"/tenants/{tid}").status_code == 302  # now goes to their account page


def test_units_not_properties(app, client):
    token = csrf(client)
    assert q(app, "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM units GROUP BY property_id)") == [1]
    home = client.get("/").get_data(as_text=True)
    assert ">Units<" in home and ">Properties<" not in home and "Add unit" in home
    listing = client.get("/properties").get_data(as_text=True)
    assert "<h1>Units</h1>" in listing and "Add tenant" in listing and "Record payment" in listing
    form = client.get("/properties/new").get_data(as_text=True)
    assert 'name="units"' not in form and "Add unit" in form
    r = client.post("/properties/new", data={"csrf_token": token, "name": "Unit 2B Sunrise Apartments"})
    pid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = client.get(f"/properties/{pid}").get_data(as_text=True)
    assert "Add a unit" not in page and "No tenant yet" in page and "Add tenant" in page
    assert client.post(f"/properties/{pid}/units", data={"csrf_token": token}).status_code in (404, 405)
    unit = q(app, f"SELECT id FROM units WHERE property_id = {pid}")[0]
    assert client.get(f"/units/{unit}").headers["Location"].endswith(f"/properties/{pid}")
    assert "the unit" in client.get(f"/delete/property/{pid}").get_data(as_text=True)


def test_old_multi_unit_properties_are_split_on_startup(app):
    from rental_tracker.services import portfolio
    conn = dbmod.connect(app.extensions["rental_tracker"].data.db)
    pid = portfolio.save_property(conn, None, {"name": "Old Building"}, unit_labels=["A", "B"])
    conn.close()
    app.extensions["rental_tracker"].last_catch_up = None  # as if the app just started
    c = app.test_client()
    c.get(f"/auth?token={TOKEN}")
    page = c.get("/properties?q=Old").get_data(as_text=True)
    assert "Old Building · A" in page and "Old Building · B" in page
    assert q(app, f"SELECT COUNT(*) FROM units WHERE property_id = {pid}") == [1]


def test_late_page_and_fees(app, client):
    token = csrf(client)
    page = client.get("/late").get_data(as_text=True)
    assert "overdue" in page
    keys = re.findall(r'name="key" value="([^"]+)"', page)
    assert keys, "demo tenants have late fees"
    client.post("/late", data={"csrf_token": token, "action": "waive", "key": keys[:1]})
    client.post("/late", data={"csrf_token": token, "action": "approve", "key": keys[1:2]})
    page = client.get("/late").get_data(as_text=True)
    assert keys[0] not in page and keys[1] not in page


def test_settings(client):
    token = csrf(client)
    client.post("/settings", data={"csrf_token": token, "business_name": "Sakai Rentals", "default_late_fee_cents": "50",
                                   "default_grace_days": "3", "late_fee_mode": "review"})
    page = client.get("/settings").get_data(as_text=True)
    assert "Sakai Rentals" in page and 'value="50.00"' in page
    assert 'value="50.00"' in client.get("/tenants/new").get_data(as_text=True)  # used for new tenants


def test_quit_button(app, client):
    token = csrf(client)
    assert client.post("/quit", data={"csrf_token": token}).status_code == 404  # not started by the launcher
    called = []
    app.extensions["rental_tracker"].on_quit = lambda: called.append(True)
    assert "Quit" in client.get("/").get_data(as_text=True)
    resp = client.post("/quit", data={"csrf_token": token})
    assert resp.status_code == 200 and "has closed" in resp.get_data(as_text=True)
    time.sleep(0.5)
    assert called == [True]


def test_no_back_button_or_vacant_units(app):
    c = app.test_client()
    c.get(f"/auth?token={TOKEN}&window=1")
    assert "data-back" not in c.get("/").get_data(as_text=True)
    assert "← Back" not in c.get(f"/payments/{q(app, 'SELECT id FROM payments LIMIT 1')[0]}/receipt").get_data(as_text=True)
    props = c.get("/properties").get_data(as_text=True)
    assert 'name="vacant"' not in props and "vacant" not in props.lower()
    home = c.get("/").get_data(as_text=True)
    assert "Vacant" not in home and "Occupied" not in home
    assert "vacant" not in c.get("/reports/rent-roll").get_data(as_text=True).lower()
    assert "Vacant units" not in c.get("/reports").get_data(as_text=True)


def test_payment_methods_and_no_ref(app, client):
    token = csrf(client)
    lid = q(app, "SELECT id FROM leases WHERE status = 'active' LIMIT 1")[0]
    page = client.get(f"/leases/{lid}").get_data(as_text=True)
    methods = re.findall(r'<option value="([a-z_]+)"', page.split('name="method"')[1].split("</select>")[0])
    assert methods == ["cash", "check", "bank_transfer", "gcash", "other"]
    assert 'name="reference"' not in page and 'name="method_other"' in page
    client.post(f"/leases/{lid}/payment", data={"csrf_token": token, "amount": "100", "method": "gcash"})
    client.post(f"/leases/{lid}/payment", data={"csrf_token": token, "amount": "100", "method": "other",
                                                "method_other": "  Maya   wallet "})
    client.post(f"/leases/{lid}/payment", data={"csrf_token": token, "amount": "100", "method": "cash",
                                                "method_other": "ignored"})
    rows = [tuple(r) for r in dbmod.connect(app.extensions["rental_tracker"].data.db).execute(
        f"SELECT method, method_other FROM payments WHERE lease_id = {lid} ORDER BY id DESC LIMIT 3")]
    assert rows == [("cash", None), ("other", "Maya wallet"), ("gcash", None)]
    other = q(app, f"SELECT id FROM payments WHERE lease_id = {lid} AND method = 'other' ORDER BY id DESC LIMIT 1")[0]
    assert "Maya wallet" in client.get(f"/payments/{other}/receipt").get_data(as_text=True)
    assert "Payment — Maya wallet" in client.get(f"/leases/{lid}").get_data(as_text=True)
    listing = client.get("/payments").get_data(as_text=True)
    assert "Maya wallet" in listing and "GCash" in listing and "<th>Ref</th>" not in listing
    assert "Maya wallet" in client.get("/payments?format=csv").get_data(as_text=True)
    grid = client.get("/rent-day").get_data(as_text=True)
    assert "<th>Ref</th>" not in grid and 'name="reference"' not in grid
    r = client.post("/rent-day/pay", data={"lease_id": lid, "amount": "50", "method": "other", "method_other": "Coins.ph",
                                           "period": "2026-09"}, headers={"HX-Request": "true", "X-CSRF-Token": token})
    assert "Saved" in r.get_data(as_text=True)
    assert q(app, f"SELECT method_other FROM payments WHERE lease_id = {lid} ORDER BY id DESC LIMIT 1") == ["Coins.ph"]
    assert 'value="Coins.ph"' in r.get_data(as_text=True)  # the row keeps it for next time
    client.post(f"/leases/{lid}/payment", data={"csrf_token": token, "amount": "1", "method": "money_order"})
    assert q(app, f"SELECT COUNT(*) FROM payments WHERE lease_id = {lid} AND method = 'money_order'")[0] == 0


def test_money_is_in_pesos(client):
    for page in ("/", "/properties", "/tenants", "/rent-day", "/payments", "/late", "/reports/rent-roll"):
        html = client.get(page).get_data(as_text=True)
        assert "₱" in html, page
        assert not re.search(r"\$\d", html), page


def test_delete_pages(app, client):
    token = csrf(client)
    lease_id = q(app, "SELECT id FROM leases WHERE status = 'active' LIMIT 1")[0]
    page = client.get(f"/leases/{lease_id}").get_data(as_text=True)
    assert "delete-payment" in page and "delete-charge" in page and f"/delete/lease/{lease_id}" in page
    assert client.get("/delete/nonsense/1").status_code == 404
    r = client.post(f"/delete/lease/{lease_id}", data={"csrf_token": token})
    assert r.status_code == 302 and "/properties/" in r.headers["Location"]
    assert q(app, f"SELECT COUNT(*) FROM charges WHERE lease_id = {lease_id}")[0] == 0
    backups = list((app.extensions["rental_tracker"].data.backups / "snapshots").glob("*before-delete-lease*"))
    assert backups
    pid = q(app, "SELECT id FROM properties LIMIT 1")[0]
    client.post(f"/delete/property/{pid}", data={"csrf_token": token})
    assert q(app, f"SELECT COUNT(*) FROM properties WHERE id = {pid}")[0] == 0


def test_delete_single_entries(app, client):
    token = csrf(client)
    pay = q(app, "SELECT id FROM payments LIMIT 1")[0]
    lease = q(app, f"SELECT lease_id FROM payments WHERE id = {pay}")[0]
    other = q(app, f"SELECT id FROM leases WHERE id <> {lease} LIMIT 1")[0]
    assert client.post(f"/leases/{other}/delete-payment/{pay}", data={"csrf_token": token}).status_code == 404
    client.post(f"/leases/{lease}/delete-payment/{pay}", data={"csrf_token": token})
    assert q(app, f"SELECT COUNT(*) FROM payments WHERE id = {pay}")[0] == 0
    pay2 = q(app, "SELECT id FROM payments LIMIT 1")[0]
    r = client.post(f"/payments/{pay2}/delete", data={"csrf_token": token, "next": "/payments?start=2026-01-01"})
    assert r.headers["Location"].endswith("/payments?start=2026-01-01")
    assert q(app, "SELECT COUNT(*) FROM audit_log WHERE action = 'delete'")[0] >= 2


def test_security_headers_and_csv_safety(client):
    resp = client.get("/")
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    from rental_tracker.services.common import csv_safe
    assert csv_safe("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert csv_safe("-12.50") == "-12.50" and csv_safe("Ann") == "Ann"
