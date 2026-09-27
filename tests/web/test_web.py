import re
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


def ids(app, sql):
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
    assert client.get("/", headers={"Host": "127.0.0.1:5000"}).status_code != 400  # allowed host (cookie is per-host)


def test_post_requires_csrf(client):
    assert client.post("/backups/create").status_code == 400
    assert client.post("/backups/create", data={"csrf_token": csrf(client)}).status_code == 302


def test_every_page_renders(app, client):
    pid = ids(app, "SELECT id FROM properties LIMIT 1")[0]
    unit = ids(app, "SELECT id FROM units LIMIT 1")[0]
    tenant = ids(app, "SELECT id FROM tenants LIMIT 1")[0]
    lease_ids = ids(app, "SELECT id FROM leases")
    payment = ids(app, "SELECT id FROM payments LIMIT 1")[0]
    expense = ids(app, "SELECT id FROM expenses LIMIT 1")[0]
    vendor = ids(app, "SELECT id FROM vendors LIMIT 1")[0]
    owner = ids(app, "SELECT id FROM owners LIMIT 1")[0]
    pages = ["/", "/search?q=maple", "/search?q=zzzz", "/properties", "/properties?format=csv", "/properties/new",
             f"/properties/{pid}", f"/properties/{pid}/edit", f"/units/{unit}", "/owners", f"/owners/{owner}",
             "/tenants", "/tenants?status=all", "/tenants/new", f"/tenants/{tenant}", f"/tenants/{tenant}/edit",
             "/leases", "/leases?status=all", "/leases?status=active&expiring=60", "/leases/new",
             f"/leases/new?unit_id={unit}", "/rent-day", "/rent-day?period=2026-08&show=unpaid", "/late-fees",
             "/payments", "/payments?format=csv", f"/payments/{payment}/receipt", "/expenses",
             "/expenses?format=csv", f"/expenses/{expense}", "/vendors", "/vendors/new", f"/vendors/{vendor}",
             f"/vendors/{vendor}/edit", "/reports", "/settings", "/settings/categories", "/backups", "/import",
             "/import/template/leases.csv", "/audit"]
    from rental_tracker.services.reports import REPORTS
    pages += [f"/reports/{k}" for k in REPORTS] + [f"/reports/{k}?format=csv" for k in REPORTS]
    for lid in lease_ids:
        pages += [f"/leases/{lid}", f"/leases/{lid}/statement"]
    pages.append(f"/leases/{lease_ids[0]}/edit")
    for page in pages:
        resp = client.get(page)
        assert resp.status_code in (200, 302), f"{page} -> {resp.status_code}"
    assert client.get("/properties/999999").status_code == 404
    assert client.get("/leases/999999").status_code == 404


def test_no_external_urls_in_templates_or_static():
    root = Path(__file__).resolve().parents[2] / "src" / "rental_tracker" / "web"
    for path in list((root / "templates").rglob("*.html")) + [root / "static" / "app.css", root / "static" / "app.js"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(src|href)=[\"']https?://", text), path
        assert "@import url(" not in text, path


def test_full_workflow(app, client):
    token = csrf(client)
    # owner + property with two units
    r = client.post("/properties/new", data={"csrf_token": token, "owner_id": "1", "code": "test-1", "name": "Test duplex",
                                             "property_type": "multi_family", "address_line1": "1 Test St",
                                             "city": "Springfield", "state": "IL", "postal_code": "62701",
                                             "units": "A, B", "tags": "Test tag"})
    assert r.status_code == 302, r.get_data(as_text=True)[:2000]
    pid = ids(app, "SELECT id FROM properties WHERE code = 'TEST-1'")[0]
    unit_a = ids(app, f"SELECT id FROM units WHERE property_id = {pid} AND unit_label = 'A'")[0]
    # lease with a brand-new tenant and a deposit
    r = client.post("/leases/new", data={"csrf_token": token, "unit_id": unit_a, "start_date": "2026-09-15",
                                         "end_date": "2027-09-14", "tenant_1": "Rita Moreno", "role_1": "primary",
                                         "phone_1": "555-1234", "rent": "1,200", "rent_due_day": "1",
                                         "prorate_partial_months": "1", "deposit": "1200", "deposit_received": "1200",
                                         "late_fee_type": "flat", "late_fee_flat": "50", "late_fee_grace_days": "5"})
    assert r.status_code == 302, r.get_data(as_text=True)[:3000]
    lid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    page = client.get(f"/leases/{lid}").get_data(as_text=True)
    assert "Rita Moreno" in page and "prorated" in page  # September prorated (16 of 30 days)
    # Rent Day HTMX payment
    r = client.post("/rent-day/pay", data={"lease_id": lid, "amount": "640.00", "received_date": "2026-09-20",
                                           "method": "check", "reference": "101", "period": "2026-09"},
                    headers={"HX-Request": "true", "X-CSRF-Token": token})
    assert r.status_code == 200 and "Saved $640.00" in r.get_data(as_text=True)
    # bad amount shows an inline error, not a crash
    r = client.post("/rent-day/pay", data={"lease_id": lid, "amount": "abc", "received_date": "2026-09-20",
                                           "method": "check", "period": "2026-09"},
                    headers={"HX-Request": "true", "X-CSRF-Token": token})
    assert r.status_code == 200 and "row-error" in r.get_data(as_text=True)
    # void the payment as NSF with a fee
    pay_id = ids(app, f"SELECT id FROM payments WHERE lease_id = {lid}")[0]
    r = client.post(f"/leases/{lid}/void-payment/{pay_id}", data={"csrf_token": token, "reason": "NSF", "nsf_fee": "35"})
    assert r.status_code == 302
    assert ids(app, f"SELECT balance_cents FROM v_lease_balances WHERE lease_id = {lid}")[0] == 64000 + 3500
    # expense with a receipt upload creates the vendor
    from io import BytesIO
    cat = ids(app, "SELECT id FROM expense_categories WHERE name = 'Repairs'")[0]
    r = client.post("/expenses", data={"csrf_token": token, "expense_date": "2026-09-21", "amount": "180",
                                       "category_id": cat, "property_id": pid, "vendor": "Brand New Plumbing",
                                       "receipt": (BytesIO(b"%PDF-1.4 fake"), "receipt.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    assert ids(app, "SELECT COUNT(*) FROM vendors WHERE name = 'Brand New Plumbing'")[0] == 1
    doc = ids(app, "SELECT id FROM documents WHERE related_type = 'expense'")[0]
    assert client.get(f"/documents/{doc}").status_code == 200
    # validation errors re-render the form with a message
    r = client.post("/properties/new", data={"csrf_token": token, "owner_id": "1", "code": "", "name": ""})
    assert r.status_code == 200 and "is required" in r.get_data(as_text=True)


def test_late_fee_review_and_settings(app, client):
    token = csrf(client)
    page = client.get("/late-fees").get_data(as_text=True)
    keys = re.findall(r'name="key" value="([^"]+)"', page)
    assert keys, "demo data should have late fees to review"
    r = client.post("/late-fees", data={"csrf_token": token, "action": "waive", "key": keys[:1]})
    assert r.status_code == 302
    assert keys[0] not in client.get("/late-fees").get_data(as_text=True)
    r = client.post("/settings", data={"csrf_token": token, "rent_post_days_before_due": "5",
                                       "proration_method": "actual_days", "late_fee_mode": "review",
                                       "late_fee_min_balance_cents": "10", "payment_application_order": "oldest_first",
                                       "expired_lease_action": "month_to_month", "deposit_return_days": "21",
                                       "books_locked_through": "", "receipt_number_prefix": "RCPT-",
                                       "backup_keep_daily": "14", "backup_keep_weekly": "8", "backup_keep_monthly": "24"})
    assert r.status_code == 302
    assert 'value="21"' in client.get("/settings").get_data(as_text=True)


def test_import_flow(app, client):
    from io import BytesIO
    token = csrf(client)
    data = {"csrf_token": token,
            "tenants": (BytesIO(b"tenant_key,first_name,last_name\nNEW-1,Test,Person\n"), "tenants.csv")}
    page = client.post("/import", data=data, content_type="multipart/form-data").get_data(as_text=True)
    assert "Check passed" in page
    tok = re.search(r'name="token" value="([^"]+)"', page).group(1)
    page = client.post("/import/commit", data={"csrf_token": token, "token": tok}).get_data(as_text=True)
    assert "Imported" in page
    assert ids(app, "SELECT COUNT(*) FROM tenants WHERE external_ref = 'NEW-1'")[0] == 1


def test_backup_and_restore_pages(app, client):
    token = csrf(client)
    client.post("/backups/create", data={"csrf_token": token})
    page = client.get("/backups").get_data(as_text=True)
    name = re.search(r'name="name" value="([^"]+manual[^"]*)"', page).group(1)
    r = client.post("/backups/restore", data={"csrf_token": token, "name": name})
    assert r.status_code == 302
    assert "Restored" in client.get("/backups").get_data(as_text=True)
    r = client.post("/backups/restore", data={"csrf_token": token, "name": "../../etc/passwd"})
    assert "Backup not found" in client.get("/backups").get_data(as_text=True)


def test_rent_day_rejects_typo_amounts(app, client):
    token = csrf(client)
    lid = ids(app, "SELECT id FROM leases WHERE status = 'active' LIMIT 1")[0]
    r = client.post("/rent-day/pay", data={"lease_id": lid, "amount": "310000", "received_date": "2026-09-20",
                                           "method": "check", "period": "2026-09"},
                    headers={"HX-Request": "true", "X-CSRF-Token": token})
    assert "far more than this lease owes" in r.get_data(as_text=True)
    assert ids(app, "SELECT COUNT(*) FROM payments WHERE amount_cents = 31000000")[0] == 0


def test_security_headers_and_csv_safety(client):
    resp = client.get("/")
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    from rental_tracker.services.common import csv_safe
    assert csv_safe("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert csv_safe("-12.50") == "-12.50" and csv_safe("Ann") == "Ann"


def test_quit_button(app, client):
    token = csrf(client)
    assert client.post("/quit", data={"csrf_token": token}).status_code == 404  # not started by the launcher
    called = []
    app.extensions["rental_tracker"].on_quit = lambda: called.append(True)
    assert "Quit" in client.get("/").get_data(as_text=True)
    resp = client.post("/quit", data={"csrf_token": token})
    assert resp.status_code == 200 and "has closed" in resp.get_data(as_text=True)
    import time
    time.sleep(0.5)
    assert called == [True]


def test_window_mode_shows_back_button(app):
    c = app.test_client()
    c.get(f"/auth?token={TOKEN}&window=1")
    assert "data-back" in c.get("/").get_data(as_text=True)


def test_delete_pages_and_buttons(app, client):
    token = csrf(client)
    lease_id = ids(app, "SELECT id FROM leases WHERE status = 'active' LIMIT 1")[0]
    page = client.get(f"/leases/{lease_id}").get_data(as_text=True)
    assert "Delete lease" in page and "delete-payment" in page and "delete-charge" in page
    # every record type has a confirm page that lists what goes
    for kind, sql in [("owner", "SELECT id FROM owners"), ("property", "SELECT id FROM properties"),
                      ("unit", "SELECT id FROM units"), ("lease", "SELECT id FROM leases"),
                      ("tenant", "SELECT id FROM tenants"), ("vendor", "SELECT id FROM vendors"),
                      ("category", "SELECT id FROM expense_categories")]:
        rid = ids(app, sql + " LIMIT 1")[0]
        resp = client.get(f"/delete/{kind}/{rid}")
        assert resp.status_code == 200 and ("permanently removes" in resp.get_data(as_text=True)
                                            or "can't be deleted yet" in resp.get_data(as_text=True)), kind
    assert client.get("/delete/nonsense/1").status_code == 404
    assert client.get("/delete/property/999999").status_code == 404
    # a lease with money needs the typed word, then goes, with a safety backup first
    r = client.post(f"/delete/lease/{lease_id}", data={"csrf_token": token, "confirm": "nope"})
    assert "Type DELETE" in r.get_data(as_text=True)
    assert ids(app, f"SELECT COUNT(*) FROM leases WHERE id = {lease_id}")[0] == 1
    r = client.post(f"/delete/lease/{lease_id}", data={"csrf_token": token, "confirm": "DELETE"})
    assert r.status_code == 302 and "/units/" in r.headers["Location"]
    assert ids(app, f"SELECT COUNT(*) FROM charges WHERE lease_id = {lease_id}")[0] == 0
    backups = list((app.extensions["rental_tracker"].data.backups / "snapshots").glob("*before-delete-lease*"))
    assert backups


def test_delete_single_entries(app, client):
    token = csrf(client)
    pay = ids(app, "SELECT id FROM payments WHERE voided_at IS NULL LIMIT 1")[0]
    lease = ids(app, f"SELECT lease_id FROM payments WHERE id = {pay}")[0]
    other = ids(app, f"SELECT id FROM leases WHERE id <> {lease} LIMIT 1")[0]
    assert client.post(f"/leases/{other}/delete-payment/{pay}", data={"csrf_token": token}).status_code == 404
    client.post(f"/leases/{lease}/delete-payment/{pay}", data={"csrf_token": token})
    assert ids(app, f"SELECT COUNT(*) FROM payments WHERE id = {pay}")[0] == 0
    pay2 = ids(app, "SELECT id FROM payments LIMIT 1")[0]
    r = client.post(f"/payments/{pay2}/delete", data={"csrf_token": token, "next": "/payments?start=2026-01-01"})
    assert r.headers["Location"].endswith("/payments?start=2026-01-01")
    exp = ids(app, "SELECT id FROM expenses LIMIT 1")[0]
    client.post(f"/expenses/{exp}/delete", data={"csrf_token": token})
    assert ids(app, f"SELECT COUNT(*) FROM expenses WHERE id = {exp}")[0] == 0
    assert ids(app, "SELECT COUNT(*) FROM audit_log WHERE action = 'delete'")[0] >= 3
