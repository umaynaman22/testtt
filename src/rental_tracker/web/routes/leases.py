"""Leases and everything on a tenant's ledger."""
from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ...domain.money import format_money
from ...domain.periods import add_months, parse_date
from ...services import documents, leases, ledger, portfolio, rent_posting, tenants
from ...services.common import get_setting
from .. import attempt, db, today
from ..forms import Form, values_from
from . import options

bp = Blueprint("leases", __name__)

TERM_MONEY = {"late_fee_flat": "late_fee_flat_cents", "late_fee_max": "late_fee_max_cents", "deposit": "deposit_cents"}


def _terms(f: Form) -> dict:
    fee_type = f.choice("late_fee_type", "Late fee type", leases.LATE_FEE_TYPES, "none")
    return {"rent_due_day": f.int("rent_due_day", "Rent due day", required=True, lo=1, hi=28),
            "prorate_partial_months": f.bool("prorate_partial_months"),
            "deposit_cents": f.money("deposit", "Deposit") or 0,
            "late_fee_type": fee_type,
            "late_fee_grace_days": f.int("late_fee_grace_days", "Grace days", lo=0, hi=60) or 0,
            "late_fee_flat_cents": f.money("late_fee_flat", "Flat late fee") if fee_type == "flat" else None,
            "late_fee_percent_bp": f.percent_bp("late_fee_percent", "Late fee %") if fee_type == "percent" else None,
            "late_fee_max_cents": f.money("late_fee_max", "Late fee cap"),
            "notes": f.str("notes")}


def _tenant_options():
    return [(f"{t['last_name']}, {t['first_name']} #{t['id']}") for t in tenants.tenant_choices(db())]


@bp.route("/leases")
def index():
    status = request.args.get("status", "current")
    expiring = request.args.get("expiring", type=int)
    rows = leases.list_leases(db(), status=status, q=request.args.get("q", ""), expiring_days=expiring, today=today())
    return render_template("leases/index.html", rows=rows, status=status, expiring=expiring)


@bp.route("/leases/new", methods=["GET", "POST"])
def new():
    conn = db()
    unit_id = request.values.get("unit_id", type=int)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Lease created") as r:
            unit = f.id("unit_id")
            start = f.date("start_date", "Start date", required=True)
            end = f.date("end_date", "End date")
            rent = f.money("rent", "Monthly rent", required=True)
            terms = _terms(f)
            dep_received = f.money("deposit_received", "Deposit received")
            dep_date = f.date("deposit_received_date", "Deposit date")
            if not unit:
                f.errors.append("Choose a unit")
            f.check()
            slots = []
            for i in (1, 2, 3):
                tid = tenants.resolve_tenant(conn, f.raw(f"tenant_{i}"),
                                             phone=f.raw(f"phone_{i}"), email=f.raw(f"email_{i}"))
                if tid:
                    slots.append((tid, f.raw(f"role_{i}") or ("primary" if i == 1 else "co_tenant")))
            lease_id = leases.create_lease(conn, unit_id=unit, tenants=slots, start=start, end=end, rent_cents=rent,
                                           today=today(), draft=bool(f.bool("draft")), move_in_date=start, **terms)
            if dep_received:
                ledger.record_deposit(conn, lease_id, "received", dep_received, dep_date or start,
                                      "Security deposit")
            r["id"] = lease_id
        if r["done"]:
            return redirect(url_for("leases.detail", lease_id=r["id"]))
        values = request.form
    else:
        t = today()
        start = (t.replace(day=1) + timedelta(days=32)).replace(day=1)
        values = {"unit_id": unit_id or "", "start_date": start.isoformat(),
                  "end_date": (add_months(start, 12) - timedelta(days=1)).isoformat(), "rent_due_day": "1",
                  "prorate_partial_months": "1", "late_fee_type": "flat", "late_fee_grace_days": "5",
                  "role_1": "primary", "role_2": "co_tenant", "role_3": "co_tenant"}
        if unit_id:
            u = portfolio.get_unit(conn, unit_id)
            if u["market_rent_cents"]:
                values["rent"] = f"{u['market_rent_cents'] / 100:.2f}"
    units = portfolio.unit_choices(conn)
    unit_opts = [(u["id"], f"{u['code']} · {u['unit_label']}" + (" (occupied)" if u["occupied"] else ""))
                 for u in units]
    return render_template("leases/new.html", values=values, unit_opts=unit_opts, tenant_opts=_tenant_options(),
                           roles=options(leases.ROLES, {"co_tenant": "Co-tenant"}),
                           fee_types=options(leases.LATE_FEE_TYPES, {"none": "No late fee", "flat": "Flat amount",
                                                                      "percent": "Percent of rent"}))


@bp.route("/leases/<int:lease_id>")
def detail(lease_id: int):
    conn = db()
    lease = leases.get_lease(conn, lease_id)
    renewal = leases.renewal_of(conn, lease_id)
    prev = ledger.lease_row(conn, lease["renewal_of_lease_id"]) if lease["renewal_of_lease_id"] else None
    t = today()
    default_renew_start = (parse_date(lease["end_date"]) + timedelta(days=1)) if lease["end_date"] else t
    return render_template(
        "leases/detail.html", lease=lease, tenants=leases.lease_tenants(conn, lease_id),
        names=leases.tenant_names(conn, lease_id),
        entries=ledger.ledger_entries(conn, lease_id), summary=ledger.lease_summary(conn, lease_id, t),
        deposits=ledger.deposit_transactions(conn, lease_id), changes=leases.rent_changes(conn, lease_id),
        recurring=leases.recurring_charges(conn, lease_id), docs=documents.for_entity(conn, "lease", lease_id),
        renewal=renewal, prev=prev, values={}, tenant_opts=_tenant_options(),
        methods=options(ledger.PAYMENT_METHODS, {"app_transfer": "App (Zelle, Venmo…)", "card": "Card"}),
        charge_types=options(ledger.MANUAL_CHARGE_TYPES, {"nsf_fee": "NSF fee"}),
        deposit_types=options(ledger.DEPOSIT_TYPES, {"applied_to_balance": "Apply to unpaid balance",
                                                      "deduction": "Deduction (kept for damages)"}),
        recurring_types=options(leases.RECURRING_TYPES), roles=options(leases.ROLES, {"co_tenant": "Co-tenant"}),
        renew_start=default_renew_start.isoformat(),
        renew_end=(add_months(default_renew_start, 12) - timedelta(days=1)).isoformat(),
        deposit_days=get_setting(conn, "deposit_return_days", "30"))


def _back(lease_id: int, anchor: str = ""):
    return redirect(url_for("leases.detail", lease_id=lease_id) + (f"#{anchor}" if anchor else ""))


@bp.route("/leases/<int:lease_id>/payment", methods=["POST"])
def payment(lease_id: int):
    f = Form(request.form)
    with attempt("Payment recorded") as r:
        amount = f.money("amount", "Amount", required=True)
        when = f.date("received_date", "Date received", required=True)
        method = f.choice("method", "Method", ledger.PAYMENT_METHODS)
        f.check()
        r["id"] = ledger.record_payment(db(), lease_id, amount, when, method, f.str("reference"), f.str("notes"))
    if r["done"] and f.bool("print_receipt"):
        return redirect(url_for("rentday.receipt", payment_id=r["id"]))
    return _back(lease_id, "ledger")


@bp.route("/leases/<int:lease_id>/charge", methods=["POST"])
def charge(lease_id: int):
    f = Form(request.form)
    with attempt("Charge added"):
        ctype = f.choice("charge_type", "Charge type", ledger.MANUAL_CHARGE_TYPES)
        amount = f.money("amount", "Amount", required=True)
        due = f.date("due_date", "Due date", required=True)
        f.check()
        ledger.add_charge(db(), lease_id, ctype, amount, due, f.str("description"))
    return _back(lease_id, "ledger")


@bp.route("/leases/<int:lease_id>/credit", methods=["POST"])
def credit(lease_id: int):
    f = Form(request.form)
    with attempt("Credit added"):
        amount = f.money("amount", "Amount", required=True)
        when = f.date("date", "Date", required=True)
        f.check()
        ledger.add_credit(db(), lease_id, amount, when, f.raw("description"))
    return _back(lease_id, "ledger")


@bp.route("/leases/<int:lease_id>/void-charge/<int:charge_id>", methods=["POST"])
def void_charge(lease_id: int, charge_id: int):
    with attempt("Charge voided"):
        ledger.void_charge(db(), charge_id, request.form.get("reason", ""))
    return _back(lease_id, "ledger")


@bp.route("/leases/<int:lease_id>/void-payment/<int:payment_id>", methods=["POST"])
def void_payment(lease_id: int, payment_id: int):
    f = Form(request.form)
    with attempt("Payment voided"):
        fee = f.money("nsf_fee", "Returned-payment fee")
        f.check()
        ledger.void_payment(db(), payment_id, f.raw("reason"), nsf_fee=fee, today=today())
    return _back(lease_id, "ledger")


@bp.route("/leases/<int:lease_id>/deposit", methods=["POST"])
def deposit(lease_id: int):
    f = Form(request.form)
    with attempt("Deposit updated"):
        ttype = f.choice("txn_type", "Type", ledger.DEPOSIT_TYPES)
        amount = f.money("amount", "Amount", required=True)
        when = f.date("date", "Date", required=True)
        f.check()
        ledger.record_deposit(db(), lease_id, ttype, amount, when, f.str("description"))
    return _back(lease_id, "deposit")


@bp.route("/leases/<int:lease_id>/void-deposit/<int:txn_id>", methods=["POST"])
def void_deposit(lease_id: int, txn_id: int):
    with attempt("Deposit entry voided"):
        ledger.void_deposit(db(), txn_id, request.form.get("reason", ""))
    return _back(lease_id, "deposit")


@bp.route("/leases/<int:lease_id>/rent-change", methods=["POST"])
def rent_change(lease_id: int):
    f = Form(request.form)
    with attempt("Rent change saved. Rent already billed is not changed."):
        eff = f.date("effective_date", "Effective date", required=True)
        rent = f.money("rent", "New rent", required=True)
        sent = f.date("notice_sent_date", "Notice sent")
        f.check()
        leases.add_rent_change(db(), lease_id, eff, rent, sent, f.str("reason"))
        rent_posting.post_rent(db(), today(), [lease_id])
    return _back(lease_id, "terms")


@bp.route("/leases/<int:lease_id>/recurring", methods=["POST"])
def recurring(lease_id: int):
    f = Form(request.form)
    with attempt("Recurring charge added"):
        ctype = f.choice("charge_type", "Type", leases.RECURRING_TYPES)
        amount = f.money("amount", "Amount", required=True)
        start = f.date("start_date", "Start", required=True)
        end = f.date("end_date", "End")
        f.check()
        leases.add_recurring_charge(db(), lease_id, charge_type=ctype, description=f.raw("description"),
                                    amount_cents=amount, start=start, end=end)
        rent_posting.post_rent(db(), today(), [lease_id])
    return _back(lease_id, "terms")


@bp.route("/leases/<int:lease_id>/recurring/<int:rc_id>/end", methods=["POST"])
def end_recurring(lease_id: int, rc_id: int):
    f = Form(request.form)
    with attempt("Recurring charge ended"):
        end = f.date("end_date", "End date", required=True)
        f.check()
        leases.end_recurring_charge(db(), rc_id, end)
    return _back(lease_id, "terms")


@bp.route("/leases/<int:lease_id>/tenants", methods=["POST"])
def add_tenant(lease_id: int):
    f = Form(request.form)
    with attempt("Tenant added to lease"):
        role = f.choice("role", "Role", leases.ROLES)
        f.check()
        tid = tenants.resolve_tenant(db(), f.raw("tenant"), phone=f.raw("phone"), email=f.raw("email"))
        if not tid:
            raise ValueError("Choose or type a tenant")
        if db().execute("SELECT 1 FROM lease_tenants WHERE lease_id = ? AND tenant_id = ?", (lease_id, tid)).fetchone():
            raise ValueError("That tenant is already on this lease")
        db().execute("INSERT INTO lease_tenants(lease_id, tenant_id, role) VALUES (?, ?, ?)", (lease_id, tid, role))
    return _back(lease_id)


@bp.route("/leases/<int:lease_id>/tenants/<int:tid>/remove", methods=["POST"])
def remove_tenant(lease_id: int, tid: int):
    with attempt("Removed from lease"):
        rows = leases.lease_tenants(db(), lease_id)
        if len(rows) <= 1:
            raise ValueError("A lease needs at least one tenant")
        db().execute("DELETE FROM lease_tenants WHERE lease_id = ? AND tenant_id = ?", (lease_id, tid))
    return _back(lease_id)


@bp.route("/leases/<int:lease_id>/edit", methods=["GET", "POST"])
def edit(lease_id: int):
    conn = db()
    lease = leases.get_lease(conn, lease_id)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Lease terms saved") as r:
            fields = _terms(f)
            fields["end_date"] = f.date("end_date", "End date")
            fields["move_in_date"] = f.date("move_in_date", "Move-in date")
            rent = f.money("rent", "Starting rent", required=True)
            if rent is not None:
                fields["rent_cents"] = rent
            f.check()
            leases.update_terms(conn, lease_id, fields)
        if r["done"]:
            return _back(lease_id)
        values = request.form
    else:
        values = values_from(lease, {**TERM_MONEY, "rent": "rent_cents"}, {"late_fee_percent": "late_fee_percent_bp"})
    return render_template("leases/edit.html", lease=lease, values=values,
                           fee_types=options(leases.LATE_FEE_TYPES, {"none": "No late fee", "flat": "Flat amount",
                                                                      "percent": "Percent of rent"}))


@bp.route("/leases/<int:lease_id>/notice", methods=["POST"])
def notice(lease_id: int):
    f = Form(request.form)
    with attempt("Notice recorded. Billing stops after the move-out date."):
        given = f.date("notice_date", "Notice date", required=True)
        out = f.date("move_out_date", "Move-out date", required=True)
        f.check()
        leases.give_notice(db(), lease_id, given, out)
    return _back(lease_id)


@bp.route("/leases/<int:lease_id>/move-out", methods=["POST"])
def move_out(lease_id: int):
    f = Form(request.form)
    with attempt() as r:
        out = f.date("move_out_date", "Move-out date", required=True)
        status = f.choice("status", "End type", ("ended", "terminated"), "ended")
        f.check()
        r["res"] = leases.end_lease(db(), lease_id, out, status, f.str("reason"))
    if r["done"]:
        res = r["res"]
        msg = "Move-out recorded."
        if res.voided or res.credit_cents:
            msg += f" Voided {res.voided} charge(s) after move-out; prorated credit {format_money(res.credit_cents)}."
        msg += " Next: settle the security deposit."
        flash(msg, "ok")
    return _back(lease_id, "deposit")


@bp.route("/leases/<int:lease_id>/renew", methods=["POST"])
def renew(lease_id: int):
    f = Form(request.form)
    with attempt("Renewal created") as r:
        start = f.date("start_date", "Start", required=True)
        end = f.date("end_date", "End")
        rent = f.money("rent", "Rent", required=True)
        f.check()
        r["id"] = leases.renew_lease(db(), lease_id, start=start, end=end, rent_cents=rent, today=today())
    return redirect(url_for("leases.detail", lease_id=r["id"])) if r["done"] else _back(lease_id)


@bp.route("/leases/<int:lease_id>/sign", methods=["POST"])
def sign(lease_id: int):
    with attempt("Lease marked as signed"):
        leases.sign_draft(db(), lease_id, today())
    return _back(lease_id)


@bp.route("/leases/<int:lease_id>/delete-draft", methods=["POST"])
def delete_draft(lease_id: int):
    unit_id = ledger.lease_row(db(), lease_id)["unit_id"]
    with attempt("Draft deleted") as r:
        leases.delete_draft(db(), lease_id)
    return redirect(url_for("properties.unit_detail", unit_id=unit_id)) if r["done"] else _back(lease_id)


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
    owner = conn.execute("SELECT o.* FROM owners o JOIN properties p ON p.owner_id = o.id WHERE p.id = ?",
                         (lease["property_id"],)).fetchone()
    return render_template("leases/statement.html", lease=lease, entries=[e for e in entries if not e["voided_at"]],
                           opening=opening, start=start, owner=owner, names=leases.tenant_names(conn, lease_id),
                           summary=ledger.lease_summary(conn, lease_id, today()))
