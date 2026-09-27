"""A tenant's account: adding a tenant to a property, payments, balance, moving out.

Behind the scenes each tenancy is a lease (a unit + the people living there);
the screens just call it the tenant's page.
"""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ...domain.money import cents_to_input
from ...domain.periods import due_date, parse_date, period_of, period_start
from ...services import deletion, leases, ledger, portfolio, rent_posting, tenants
from ...services.common import get_int_setting, get_setting
from .. import attempt, db, today
from ..forms import Form
from . import options
from .tenants import person_fields

bp = Blueprint("leases", __name__)
METHOD_LABELS = {"app_transfer": "App (Zelle, Venmo…)", "card": "Card", "housing_assistance": "Housing assistance"}


def _unit_options(conn, keep_unit: int | None = None):
    opts = []
    for u in portfolio.unit_choices(conn):
        if u["occupied"] and u["id"] != keep_unit:
            continue
        label = u["code"] if u["unit_label"] == "Main" else f"{u['code']} · {u['unit_label']}"
        opts.append((u["id"], label))
    return opts


def _late_fee_terms(f: Form, conn) -> dict:
    fee = f.money("late_fee", "Late fee")
    grace = f.int("grace_days", "Days before it's late", lo=0, hi=60)
    return {"late_fee_type": "flat" if fee else "none", "late_fee_flat_cents": fee or None,
            "late_fee_grace_days": get_int_setting(conn, "default_grace_days", 5) if grace is None else grace}


def _next_due(lease) -> date:
    t = today()
    due = due_date(period_of(t), lease["rent_due_day"])
    return due if due > t else due_date(period_of(due + timedelta(days=31)), lease["rent_due_day"])


@bp.route("/tenants/new", methods=["GET", "POST"])
def new():
    """Add a tenant to a property. Everything is optional."""
    conn = db()
    unit_id = request.values.get("unit_id", type=int)
    existing = request.values.get("tenant_id", type=int)
    person = tenants.get_tenant(conn, existing) if existing else None
    t = today()
    if request.method == "POST":
        f = Form(request.form)
        with attempt() as r:
            unit = f.id("unit_id")
            start = f.date("moved_in", "Moved in") or t.isoformat()
            rent = f.money("rent", "Rent")
            due = f.int("rent_due_day", "Rent due day", lo=1, hi=28)
            owes = f.money("owes_now", "Already owes")
            deposit = f.money("deposit", "Deposit")
            billing = f.date("billing_start", "Start billing from")
            end = f.date("end_date", "Lease ends")
            fee_terms = _late_fee_terms(f, conn)
            f.check()
            tid = existing or tenants.save_tenant(conn, None, person_fields(f))
            if not unit:
                r["tenant_only"] = tid
            else:
                people = [(tid, "primary")]
                if f.raw("second_name"):
                    first, last = tenants.split_name(f.raw("second_name"))
                    people.append((tenants.save_tenant(conn, None, {"first_name": first, "last_name": last}),
                                   "co_tenant"))
                this_month = period_start(period_of(t))
                if billing is None and parse_date(start) < this_month:
                    billing = this_month.isoformat()  # don't bill years of past rent for existing tenants
                lease_id = leases.create_lease(
                    conn, unit_id=unit, tenants=people, start=start, end=end, rent_cents=rent, today=t,
                    billing_start=billing, rent_due_day=due, deposit_cents=deposit or 0, move_in_date=start,
                    notes=f.str("notes"), **fee_terms)
                if owes:
                    before = parse_date(billing or start) - timedelta(days=1)
                    ledger.add_charge(conn, lease_id, "opening_balance", owes, before.isoformat(),
                                      "Balance owed when added")
                if deposit:
                    ledger.record_deposit(conn, lease_id, "received", deposit, start, "Security deposit")
                r["lease_id"] = lease_id
        if r["done"]:
            if r.get("lease_id"):
                flash("Tenant added.", "ok")
                return redirect(url_for("leases.detail", lease_id=r["lease_id"]))
            flash("Tenant added. You can link them to a property any time.", "ok")
            return redirect(url_for("tenants.detail", tid=r["tenant_only"]))
        values = request.form
    else:
        values = {"unit_id": unit_id or "", "moved_in": t.isoformat(), "rent_due_day": "1",
                  "grace_days": str(get_int_setting(conn, "default_grace_days", 5))}
        fee = get_int_setting(conn, "default_late_fee_cents", 0)
        if fee:
            values["late_fee"] = cents_to_input(fee)
        if unit_id:
            u = portfolio.get_unit(conn, unit_id)
            if u["market_rent_cents"]:
                values["rent"] = cents_to_input(u["market_rent_cents"])
    return render_template("leases/new.html", values=values, person=person,
                           unit_opts=_unit_options(conn, keep_unit=unit_id))


@bp.route("/leases/<int:lease_id>")
def detail(lease_id: int):
    conn = db()
    lease = leases.get_lease(conn, lease_id)
    entries = ledger.ledger_entries(conn, lease_id)
    payments = [e for e in entries if e["kind"] == "payment" and not e["voided_at"]]
    return render_template(
        "leases/detail.html", lease=lease, people=leases.lease_tenants(conn, lease_id),
        names=leases.tenant_names(conn, lease_id) or "Tenant",
        entries=list(reversed(entries)), summary=ledger.lease_summary(conn, lease_id, today()),
        last_payment=payments[-1] if payments else None,
        deposits=ledger.deposit_transactions(conn, lease_id), changes=leases.rent_changes(conn, lease_id),
        methods=options(ledger.PAYMENT_METHODS, METHOD_LABELS),
        deposit_types=options(("received", "refund", "deduction", "applied_to_balance"),
                              {"received": "Received from tenant", "refund": "Returned to tenant",
                               "deduction": "Kept (damage, cleaning…)",
                               "applied_to_balance": "Used to pay what they owe"}),
        values={})


def _back(lease_id: int, anchor: str = ""):
    return redirect(url_for("leases.detail", lease_id=lease_id) + (f"#{anchor}" if anchor else ""))


@bp.route("/leases/<int:lease_id>/payment", methods=["POST"])
def payment(lease_id: int):
    f = Form(request.form)
    with attempt("Payment saved") as r:
        amount = f.money("amount", "Amount")
        when = f.date("received_date", "Date")
        f.check()
        r["id"] = ledger.record_payment(db(), lease_id, amount, when, f.raw("method") or None,
                                        f.str("reference"), f.str("notes"))
    if r["done"] and f.bool("print_receipt"):
        return redirect(url_for("rentday.receipt", payment_id=r["id"]))
    return _back(lease_id, "history")


@bp.route("/leases/<int:lease_id>/charge", methods=["POST"])
def charge(lease_id: int):
    f = Form(request.form)
    with attempt("Charge added"):
        amount = f.money("amount", "Amount")
        due = f.date("due_date", "Date")
        f.check()
        ledger.add_charge(db(), lease_id, "other", amount, due, f.str("description"))
    return _back(lease_id, "history")


@bp.route("/leases/<int:lease_id>/credit", methods=["POST"])
def credit(lease_id: int):
    f = Form(request.form)
    with attempt("Credit added"):
        amount = f.money("amount", "Amount")
        when = f.date("date", "Date")
        f.check()
        ledger.add_credit(db(), lease_id, amount, when, f.raw("description"))
    return _back(lease_id, "history")


@bp.route("/leases/<int:lease_id>/deposit", methods=["POST"])
def deposit(lease_id: int):
    f = Form(request.form)
    with attempt("Deposit updated"):
        amount = f.money("amount", "Amount")
        when = f.date("date", "Date")
        f.check()
        ledger.record_deposit(db(), lease_id, f.raw("txn_type") or "received", amount, when, f.str("description"))
    return _back(lease_id, "deposit")


@bp.route("/leases/<int:lease_id>/rent-change", methods=["POST"])
def rent_change(lease_id: int):
    f = Form(request.form)
    with attempt("Rent changed. Rent already billed stays the same."):
        rent = f.money("rent", "New rent")
        eff = f.date("effective_date", "Starting")
        f.check()
        if rent is None:
            raise ValueError("Enter the new rent")
        eff = eff or _next_due(ledger.lease_row(db(), lease_id)).isoformat()
        leases.add_rent_change(db(), lease_id, eff, rent, None, None)
        rent_posting.post_rent(db(), today(), [lease_id])
    return _back(lease_id, "more")


@bp.route("/leases/<int:lease_id>/move-out", methods=["POST"])
def move_out(lease_id: int):
    f = Form(request.form)
    with attempt() as r:
        out = f.date("move_out_date", "Moved out") or today().isoformat()
        f.check()
        leases.end_lease(db(), lease_id, out, "ended")
    if r["done"]:
        flash("Marked as moved out. Rent billed after that day was taken off. Their history is kept.", "ok")
    return _back(lease_id)


@bp.route("/leases/<int:lease_id>/people", methods=["POST"])
def add_person(lease_id: int):
    f = Form(request.form)
    with attempt("Added"):
        first, last = tenants.split_name(f.raw("name"))
        tid = tenants.save_tenant(db(), None, {"first_name": first, "last_name": last, "phone": f.str("phone")})
        db().execute("INSERT INTO lease_tenants(lease_id, tenant_id, role) VALUES (?, ?, 'co_tenant')", (lease_id, tid))
    return _back(lease_id)


@bp.route("/leases/<int:lease_id>/people/<int:tid>/remove", methods=["POST"])
def remove_person(lease_id: int, tid: int):
    with attempt("Removed"):
        if len(leases.lease_tenants(db(), lease_id)) <= 1:
            raise ValueError("They're the only person here. Use Delete or Moved out instead.")
        role = db().execute("SELECT role FROM lease_tenants WHERE lease_id = ? AND tenant_id = ?",
                            (lease_id, tid)).fetchone()
        db().execute("DELETE FROM lease_tenants WHERE lease_id = ? AND tenant_id = ?", (lease_id, tid))
        if role and role[0] == "primary":
            db().execute("UPDATE lease_tenants SET role = 'primary' WHERE lease_id = ? AND tenant_id = "
                         "(SELECT MIN(tenant_id) FROM lease_tenants WHERE lease_id = ?)", (lease_id, lease_id))
        if not db().execute("SELECT 1 FROM lease_tenants WHERE tenant_id = ?", (tid,)).fetchone():
            deletion.delete(db(), "tenant", tid)  # not on any other lease: remove the person too
    return _back(lease_id)


@bp.route("/leases/<int:lease_id>/edit", methods=["GET", "POST"])
def edit(lease_id: int):
    conn = db()
    lease = leases.get_lease(conn, lease_id)
    main = leases.lease_tenants(conn, lease_id)[:1]
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Saved") as r:
            rent = f.money("rent", "Rent")
            fields = {"rent_due_day": f.int("rent_due_day", "Rent due day", lo=1, hi=28) or lease["rent_due_day"],
                      "notes": f.str("notes"), "move_in_date": f.date("moved_in", "Moved in"),
                      "end_date": f.date("end_date", "Lease ends"), **_late_fee_terms(f, conn)}
            person = person_fields(f)
            f.check()
            if main:
                tenants.save_tenant(conn, main[0]["id"], person)
            billed = conn.execute("SELECT COUNT(*) FROM charges WHERE lease_id = ? AND charge_type = 'rent'",
                                  (lease_id,)).fetchone()[0]
            if rent is not None and rent != lease["current_rent_cents"]:
                if billed:  # keep past bills as they were; the new rent starts with the next bill
                    eff = _next_due(lease).isoformat()
                    conn.execute("DELETE FROM lease_rent_changes WHERE lease_id = ? AND effective_date = ?",
                                 (lease_id, eff))
                    leases.add_rent_change(conn, lease_id, eff, rent)
                    flash(f"The new rent starts with the bill due {eff}.", "info")
                else:
                    fields["rent_cents"] = rent
            leases.update_terms(conn, lease_id, fields)
            rent_posting.post_rent(conn, today(), [lease_id])
        if r["done"]:
            return _back(lease_id)
        values = request.form
    else:
        values = {"rent": cents_to_input(lease["current_rent_cents"]), "rent_due_day": str(lease["rent_due_day"]),
                  "late_fee": cents_to_input(lease["late_fee_flat_cents"]) if lease["late_fee_type"] == "flat" else "",
                  "grace_days": str(lease["late_fee_grace_days"]), "notes": lease["notes"] or "",
                  "moved_in": lease["move_in_date"] or lease["start_date"], "end_date": lease["end_date"] or ""}
        if main:
            values.update(name=f"{main[0]['first_name']} {main[0]['last_name']}".strip(),
                          phone=main[0]["phone"] or "", email=main[0]["email"] or "")
    return render_template("leases/edit.html", lease=lease, values=values, has_person=bool(main))


def _delete_entry(lease_id: int, table: str, entry_id: int, action, anchor: str):
    """Delete one history/deposit/rent entry that belongs to this tenant."""
    row = db().execute(f"SELECT lease_id FROM {table} WHERE id = ?", (entry_id,)).fetchone()
    if row is None or row[0] != lease_id:
        abort(404)
    with attempt() as r:
        r["msg"] = action()
    if r["done"]:
        flash(r["msg"], "ok")
    return _back(lease_id, anchor)


@bp.route("/leases/<int:lease_id>/delete-charge/<int:charge_id>", methods=["POST"])
def delete_charge(lease_id: int, charge_id: int):
    return _delete_entry(lease_id, "charges", charge_id,
                         lambda: deletion.delete_charge(db(), charge_id, today()), "history")


@bp.route("/leases/<int:lease_id>/delete-payment/<int:payment_id>", methods=["POST"])
def delete_payment(lease_id: int, payment_id: int):
    return _delete_entry(lease_id, "payments", payment_id, lambda: deletion.delete_payment(db(), payment_id), "history")


@bp.route("/leases/<int:lease_id>/delete-deposit/<int:txn_id>", methods=["POST"])
def delete_deposit(lease_id: int, txn_id: int):
    return _delete_entry(lease_id, "deposit_transactions", txn_id,
                         lambda: deletion.delete_deposit(db(), txn_id), "deposit")


@bp.route("/leases/<int:lease_id>/rent-change/<int:change_id>/delete", methods=["POST"])
def delete_rent_change(lease_id: int, change_id: int):
    return _delete_entry(lease_id, "lease_rent_changes", change_id,
                         lambda: deletion.delete_rent_change(db(), change_id), "more")


@bp.route("/leases/<int:lease_id>/statement")
def statement(lease_id: int):
    conn = db()
    lease = leases.get_lease(conn, lease_id)
    start = request.args.get("start") or ""
    entries = ledger.ledger_entries(conn, lease_id)
    opening = 0
    if start:
        before = [e for e in entries if e["date"] < start]
        opening = before[-1]["balance"] if before else 0
        entries = [e for e in entries if e["date"] >= start]
    return render_template("leases/statement.html", lease=lease, entries=[e for e in entries if not e["voided_at"]],
                           opening=opening, start=start, names=leases.tenant_names(conn, lease_id),
                           summary=ledger.lease_summary(conn, lease_id, today()),
                           business=get_setting(conn, "business_name", ""),
                           business_contact=get_setting(conn, "business_contact", ""))
