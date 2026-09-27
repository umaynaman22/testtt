"""Expenses and vendors."""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, redirect, render_template, request, session, url_for

from ...domain.periods import period_start
from ...services import documents, expenses, portfolio
from ...services.common import csv_row
from .. import attempt, db, state, today
from ..forms import Form, values_from
from . import current_period, options

bp = Blueprint("expenses", __name__)
METHOD_LABELS = {"autopay": "Autopay / auto-debit"}


def _choices():
    conn = db()
    props = portfolio.list_properties(conn, status="all")
    units = conn.execute("""SELECT u.id, p.code, u.unit_label FROM units u JOIN properties p ON p.id = u.property_id
                             WHERE u.status <> 'archived' ORDER BY p.code, u.unit_label""").fetchall()
    return {
        "props": [(p["id"], f"{p['code']} — {p['name']}") for p in props],
        "units": [(u["id"], f"{u['code']} · {u['unit_label']}") for u in units],
        "cats": [(c["id"], c["name"] + (" (capital)" if c["is_capital"] else "")) for c in expenses.list_categories(conn, True)],
        "vendor_names": [v["name"] for v in expenses.list_vendors(conn, active_only=True)],
        "methods": options(expenses.PAYMENT_METHODS, METHOD_LABELS),
    }


@bp.route("/expenses", methods=["GET", "POST"])
def index():
    conn = db()
    a = request.args
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Expense saved") as r:
            category = f.id("category_id")
            amount = f.money("amount", "Amount", required=True)
            when = f.date("expense_date", "Date", required=True)
            method = f.raw("payment_method") or None
            f.check()
            vendor_id = expenses.find_or_create_vendor(conn, f.raw("vendor"))
            eid = expenses.create_expense(conn, category_id=category, expense_date=when, amount_cents=amount,
                                          property_id=f.id("property_id"), unit_id=f.id("unit_id"),
                                          vendor_id=vendor_id, payment_method=method,
                                          reference=f.str("reference"), description=f.str("description"))
            upload = request.files.get("receipt")
            if upload and upload.filename:
                documents.store(conn, state().data, related_type="expense", related_id=eid, filename=upload.filename,
                                content=upload.read(), title=f"Receipt — {f.raw('vendor') or 'expense'} {when}",
                                doc_type="receipt")
            r["id"] = eid
        if r["done"]:
            # Remember the last choices: entering a pile of receipts is faster.
            session["expense_defaults"] = {k: request.form.get(k, "") for k in
                                           ("category_id", "property_id", "vendor", "payment_method", "expense_date")}
            return redirect(url_for("expenses.index", **{k: v for k, v in a.items()}))
        values = request.form
    else:
        values = {"expense_date": today().isoformat(), "property_id": a.get("property", ""),
                  **session.get("expense_defaults", {})}
        if a.get("property"):
            values["property_id"] = a.get("property")
    t = today()
    start = a.get("start") or period_start(current_period(t)).replace(month=1).isoformat()
    end = a.get("end") or t.isoformat()
    pid = a.get("property", type=int)
    rows = expenses.list_expenses(conn, start=start, end=end, property_ids=[pid] if pid else None,
                                  category_id=a.get("category", type=int), vendor_id=a.get("vendor", type=int),
                                  overhead_only=a.get("overhead") == "1", limit=5000)
    if a.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "property", "unit", "category", "tax_line", "vendor", "description", "reference",
                    "method", "amount", "voided"])
        for e in rows:
            w.writerow(csv_row([e["expense_date"], e["property_code"] or "overhead", e["unit_label"] or "",
                                e["category_name"], e["tax_line"] or "", e["vendor_name"] or "", e["description"] or "",
                                e["reference"] or "", e["payment_method"] or "", f"{e['amount_cents'] / 100:.2f}",
                                "yes" if e["voided_at"] else ""]))
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=expenses-{start}-to-{end}.csv"})
    total = sum(e["amount_cents"] for e in rows if not e["voided_at"])
    return render_template("expenses/index.html", rows=rows, total=total, start=start, end=end, values=values,
                           filter_cats=expenses.list_categories(conn), **_choices())


@bp.route("/expenses/<int:expense_id>")
def detail(expense_id: int):
    conn = db()
    return render_template("expenses/detail.html", e=expenses.get_expense(conn, expense_id),
                           docs=documents.for_entity(conn, "expense", expense_id))


@bp.route("/expenses/<int:expense_id>/void", methods=["POST"])
def void(expense_id: int):
    with attempt("Expense voided"):
        expenses.void_expense(db(), expense_id, request.form.get("reason", ""))
    return redirect(url_for("expenses.detail", expense_id=expense_id))


# ---- vendors ------------------------------------------------------------------

def _vendor_fields(f: Form) -> dict:
    return {"name": f.str("name", "Name", required=True), "trade": f.str("trade"),
            "contact_name": f.str("contact_name"), "phone": f.str("phone"), "email": f.str("email"),
            "address": f.str("address"), "tax_id_last4": f.str("tax_id_last4", max_len=4),
            "needs_1099": f.bool("needs_1099"), "license_number": f.str("license_number"),
            "insurance_expires": f.date("insurance_expires", "Insurance expires"),
            "is_active": f.bool("is_active"), "notes": f.str("notes")}


@bp.route("/vendors")
def vendors():
    rows = expenses.list_vendors(db(), q=request.args.get("q", ""), year=today().year)
    return render_template("expenses/vendors.html", rows=rows)


@bp.route("/vendors/new", methods=["GET", "POST"])
def vendor_new():
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Vendor added") as r:
            fields = _vendor_fields(f)
            f.check()
            r["id"] = expenses.save_vendor(db(), None, fields)
        if r["done"]:
            return redirect(url_for("expenses.vendor_detail", vendor_id=r["id"]))
        values = request.form
    else:
        values = {"is_active": "1"}
    return render_template("expenses/vendor_form.html", vendor=None, values=values)


@bp.route("/vendors/<int:vendor_id>")
def vendor_detail(vendor_id: int):
    conn = db()
    return render_template("expenses/vendor_detail.html", v=expenses.get_vendor(conn, vendor_id),
                           rows=expenses.list_expenses(conn, vendor_id=vendor_id, limit=200),
                           docs=documents.for_entity(conn, "vendor", vendor_id))


@bp.route("/vendors/<int:vendor_id>/edit", methods=["GET", "POST"])
def vendor_edit(vendor_id: int):
    vendor = expenses.get_vendor(db(), vendor_id)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Vendor saved") as r:
            fields = _vendor_fields(f)
            f.check()
            expenses.save_vendor(db(), vendor_id, fields)
        if r["done"]:
            return redirect(url_for("expenses.vendor_detail", vendor_id=vendor_id))
        values = request.form
    else:
        values = values_from(vendor)
    return render_template("expenses/vendor_form.html", vendor=vendor, values=values)
