"""Collect rent (batch entry), who is late, the payments list and receipts."""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ... import db as dbmod
from ...domain.money import format_money
from ...domain.periods import add_periods, parse_period, period_end, period_start
from ...services import deletion, late_fees, ledger, portfolio, rent_posting, rentday, tenants
from ...services.common import csv_row, get_setting
from .. import attempt, db, today
from ..forms import Form
from . import current_period, options, safe_next

bp = Blueprint("rentday", __name__)
METHODS = options(ledger.PAYMENT_METHODS, ledger.METHOD_LABELS)


def _period_arg() -> str:
    p = request.args.get("period") or current_period(today())
    try:
        parse_period(p)
    except ValueError:
        p = current_period(today())
    return p


@bp.route("/rent-day")
def index():
    period = _period_arg()
    show = request.args.get("show", "all")
    rows = rentday.rows(db(), period, q=request.args.get("q", ""), show=show)
    totals = {"billed": sum(r["billed_cents"] for r in rows), "paid": sum(r["period_paid_cents"] for r in rows),
              "received": sum(r["paid_cents"] for r in rows),
              "owed": sum(max(r["balance_cents"], 0) for r in rows),
              "unpaid": sum(r["state"] != "paid" for r in rows)}
    return render_template("rentday/index.html", rows=rows, period=period, show=show, totals=totals,
                           prev=add_periods(period, -1), next=add_periods(period, 1),
                           methods=METHODS,
                           pay_date=min(today(), period_end(period)).isoformat()
                           if period_start(period) <= today() else period_start(period).isoformat())


@bp.route("/rent-day/pay", methods=["POST"])
def pay():
    f = Form(request.form)
    lease_id = f.id("lease_id")
    if not lease_id:
        abort(400)
    period = request.form.get("period") or current_period(today())
    error = None
    saved = None
    try:
        with dbmod.transaction(db()):
            amount = f.money("amount", "Amount")
            when = f.date("received_date", "Date")
            method = f.raw("method") or None
            f.check()
            current = rentday.rows(db(), period, lease_id=lease_id)
            if current:
                # Quick entry makes typos easy; a huge amount is almost always a slip of the finger.
                limit = 3 * max(current[0]["balance_cents"], current[0]["current_rent_cents"] or 0, 1)
                if amount > limit:
                    raise ValueError(f"{format_money(amount)} is far more than this lease owes. "
                                     "If it's right, record it on the lease page.")
            pid = ledger.record_payment(db(), lease_id, amount, when, method, method_other=f.str("method_other"))
            saved = ledger.get_payment(db(), pid)
    except ValueError as e:
        error = str(e)
    if not request.headers.get("HX-Request"):
        return redirect(url_for("rentday.index", period=period))
    row = rentday.rows(db(), period, lease_id=lease_id)[0]
    return render_template("rentday/_row.html", r=row, period=period, saved=saved, error=error,
                           methods=METHODS, pay_date=f.raw("received_date"),
                           method=f.raw("method"), method_other=f.raw("method_other"))


@bp.route("/rent-day/post-rent", methods=["POST"])
def post_rent():
    with attempt() as r:
        r["res"] = rent_posting.post_rent(db(), today())
    if r["done"]:
        res = r["res"]
        flash(f"Billed {res.posted} new charge(s) totalling {format_money(res.amount_cents)}." if res.posted
              else "Everything due is already billed.", "ok")
    return redirect(url_for("rentday.index", period=request.form.get("period")))


@bp.route("/late", methods=["GET", "POST"])
def late():
    """Who is behind on rent, and any late fees waiting to be charged."""
    if request.method == "POST":
        action = request.form.get("action")
        with attempt() as r:
            r["n"] = late_fees.apply(db(), request.form.getlist("key"), action, today())
        if r["done"]:
            flash(f"{'Charged' if action == 'approve' else 'Skipped'} {r['n']} late fee(s).", "ok")
        return redirect(url_for("rentday.late"))
    rows = [r for r in tenants.tenancies(db(), today=today(), status="all") if r["past_due_cents"] > 0]
    rows.sort(key=lambda r: (-r["days_late"], -r["past_due_cents"]))
    cands = late_fees.find_candidates(db(), today())
    return render_template("rentday/late.html", rows=rows, total=sum(r["past_due_cents"] for r in rows),
                           cands=cands, fee_total=sum(c.fee_cents for c in cands))


@bp.route("/payments")
def payments():
    t = today()
    start = request.args.get("start") or period_start(current_period(t)).isoformat()
    end = request.args.get("end") or t.isoformat()
    pid = request.args.get("property", type=int)
    rows = ledger.list_payments(db(), start=start, end=end, method=request.args.get("method") or None,
                                property_ids=[pid] if pid else None, limit=5000)
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "receipt", "property", "unit", "tenants", "method", "amount", "voided", "void_reason"])
        for r in rows:
            w.writerow(csv_row([r["received_date"], r["receipt_number"], r["property_code"], r["unit_label"],
                                r["tenants"], ledger.method_name(r["method"], r["method_other"]),
                                f"{r['amount_cents'] / 100:.2f}",
                                "yes" if r["voided_at"] else "", r["void_reason"] or ""]))
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=payments-{start}-to-{end}.csv"})
    total = sum(r["amount_cents"] for r in rows if not r["voided_at"])
    props = [(p["id"], p["code"]) for p in portfolio.list_properties(db(), status="all")]
    return render_template("rentday/payments.html", rows=rows, total=total, start=start, end=end,
                           methods=METHODS, props=props)


@bp.route("/payments/<int:payment_id>/delete", methods=["POST"])
def delete_payment(payment_id: int):
    with attempt() as r:
        r["msg"] = deletion.delete_payment(db(), payment_id)
    if r["done"]:
        flash(r["msg"], "ok")
    return redirect(safe_next(request.form.get("next"), url_for("rentday.payments")))


@bp.route("/payments/<int:payment_id>/receipt")
def receipt(payment_id: int):
    p = ledger.get_payment(db(), payment_id)
    return render_template("rentday/receipt.html", p=p, balance=ledger.lease_balance(db(), p["lease_id"]),
                           business=get_setting(db(), "business_name", ""),
                           business_contact=get_setting(db(), "business_contact", ""))
