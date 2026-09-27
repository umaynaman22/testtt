"""A tenant's account: adding a tenant to a unit, payments, debts and balance.

Behind the scenes each tenancy is a lease (a unit + the people living there);
the screens just call it the tenant's page.
"""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ...domain.money import cents_to_input, format_money
from ...domain.periods import due_date, parse_date, period_of, period_start
from ...services import deletion, leases, ledger, portfolio, rent_posting, tenants
from ...services.common import get_int_setting, get_setting
from .. import attempt, db, today
from ..forms import Form
from . import options
from .tenants import person_fields

bp = Blueprint("leases", __name__)


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


def _moved_in(lease) -> str:
    return lease["move_in_date"] or lease["start_date"]


def _next_due(lease) -> date:
    t = today()
    due = due_date(period_of(t), lease["rent_due_day"])
    return due if due > t else due_date(period_of(due + timedelta(days=31)), lease["rent_due_day"])


@bp.route("/tenants/new", methods=["GET", "POST"])
def new():
    """Add a tenant to a unit. Everything is optional."""
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
            debt = f.money("debt", "Debt")
            billing = f.date("billing_start", "Start billing from")
            fee_terms = _late_fee_terms(f, conn)
            f.check()
            tid = existing or tenants.save_tenant(conn, None, person_fields(f))
            if not unit:
                r["tenant_only"] = tid
            else:  # rent is billed from the move-in date, past months included
                lease_id = leases.create_lease(
                    conn, unit_id=unit, tenants=[(tid, "primary")], start=start, end=None, rent_cents=rent, today=t,
                    billing_start=billing, rent_due_day=due, move_in_date=start, notes=f.str("notes"), **fee_terms)
                if debt:  # dated today, so it stays owed when past rent is marked as paid
                    ledger.add_charge(conn, lease_id, "opening_balance", debt, t.isoformat(), "Debt")
                r["lease_id"] = lease_id
                r["past"] = bool(ledger.unpaid_rent(conn, lease_id, period_start(period_of(t)) - timedelta(days=1)))
        if r["done"]:
            if r.get("lease_id"):
                flash("Tenant added. Rent is billed from the move-in date. If they already paid past months, "
                      "use “Fill in past rent” below." if r["past"] else "Tenant added.", "ok")
                return redirect(url_for("leases.detail", lease_id=r["lease_id"]))
            flash("Tenant added. You can link them to a unit any time.", "ok")
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
    unpaid = ledger.unpaid_rent(conn, lease_id, today())
    this_month = period_start(period_of(today()))
    fill = None
    if any(c.due_date < this_month for c, _ in unpaid):  # unpaid rent from past months
        fill = {"count": len(unpaid), "total": sum(a for _, a in unpaid), "since": unpaid[0][0].due_date}
    return render_template(
        "leases/detail.html", lease=lease, people=leases.lease_tenants(conn, lease_id),
        names=leases.tenant_names(conn, lease_id) or "Tenant",
        entries=list(reversed(entries)), summary=ledger.lease_summary(conn, lease_id, today()),
        last_payment=payments[-1] if payments else None,
        methods=options(ledger.PAYMENT_METHODS, ledger.METHOD_LABELS), fill=fill, values={})


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
                                        notes=f.str("notes"), method_other=f.str("method_other"))
    if r["done"] and f.bool("print_receipt"):
        return redirect(url_for("rentday.receipt", payment_id=r["id"]))
    return _back(lease_id, "history")


@bp.route("/leases/<int:lease_id>/fill-rent", methods=["POST"])
def fill_rent(lease_id: int):
    """Mark rent as paid up to a date: one payment per unpaid month, dated on its due date."""
    f = Form(request.form)
    with attempt() as r:
        through = f.date("through", "Paid up to")
        f.check()
        r["n"], r["total"] = ledger.fill_rent_paid(db(), lease_id, parse_date(through) if through else today(),
                                                   today(), f.raw("method") or None, f.str("method_other"))
    if r["done"]:
        n = r["n"]
        flash(f"Marked {n} rent bill{'s' if n != 1 else ''} as paid ({format_money(r['total'])})." if n
              else "There's no unpaid rent up to that date.", "ok")
    return _back(lease_id, "history")


@bp.route("/leases/<int:lease_id>/debt", methods=["POST"])
def debt(lease_id: int):
    """Money owed besides rent. It's added to the balance like any other bill."""
    f = Form(request.form)
    with attempt("Debt added"):
        amount = f.money("amount", "Amount")
        when = f.date("date", "Date")
        f.check()
        if not amount:
            raise ValueError("Enter how much they owe")
        note = f.str("note")
        ledger.add_charge(db(), lease_id, "other", amount, when, f"Debt — {note}" if note else "Debt")
    return _back(lease_id, "history")


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
            raise ValueError("They're the only person here. Use Delete instead.")
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
                      "notes": f.str("notes"), **_late_fee_terms(f, conn)}
            moved_in = f.date("moved_in", "Moved in")
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
            if moved_in and (moved_in != _moved_in(lease) or f.bool("bill_from_move_in")):
                leases.bill_from_move_in(conn, lease_id, moved_in, today())
                flash(f"Rent is now billed from {moved_in}. If they already paid past months, "
                      "use “Fill in past rent” below.", "info")
            else:
                rent_posting.post_rent(conn, today(), [lease_id])
        if r["done"]:
            return _back(lease_id)
        values = request.form
    else:
        values = {"rent": cents_to_input(lease["current_rent_cents"]), "rent_due_day": str(lease["rent_due_day"]),
                  "late_fee": cents_to_input(lease["late_fee_flat_cents"]) if lease["late_fee_type"] == "flat" else "",
                  "grace_days": str(lease["late_fee_grace_days"]), "notes": lease["notes"] or "",
                  "moved_in": _moved_in(lease)}
        if main:
            values.update(name=f"{main[0]['first_name']} {main[0]['last_name']}".strip(),
                          phone=main[0]["phone"] or "")
    billed_from = lease["billing_start_date"]
    return render_template("leases/edit.html", lease=lease, values=values, has_person=bool(main),
                           billed_from=billed_from if billed_from and billed_from > _moved_in(lease) else None)


def _delete_entry(lease_id: int, table: str, entry_id: int, action, anchor: str):
    """Delete one line of this tenant's history."""
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
