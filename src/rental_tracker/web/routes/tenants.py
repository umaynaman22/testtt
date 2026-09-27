"""Tenants: the list, and people who aren't linked to a unit yet."""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, redirect, render_template, request, url_for

from ...domain.money import cents_to_input
from ...services import tenants
from ...services.common import csv_row
from .. import attempt, db, today
from ..forms import Form, values_from

bp = Blueprint("tenants", __name__)


def person_fields(f: Form) -> dict:
    first, last = tenants.split_name(f.raw("name"))
    return {"first_name": first, "last_name": last, "phone": f.str("phone"), "notes": f.str("notes")}


@bp.route("/tenants")
def index():
    rows = tenants.tenancies(db(), today=today(), status="all", q=request.args.get("q", ""))
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["tenant", "unit", "phone", "rent", "balance", "overdue", "days late", "last paid"])
        for r in rows:
            w.writerow(csv_row([r["names"], r["property_code"] or "", r["phone"] or "",
                                cents_to_input(r["current_rent_cents"]),
                                cents_to_input(r["balance_cents"]), cents_to_input(r["past_due_cents"]),
                                r["days_late"] or "", r["last_paid_on"] or ""]))
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=tenants.csv"})
    return render_template("tenants/index.html", rows=rows,
                           owed=sum(max(r["balance_cents"], 0) for r in rows))


@bp.route("/tenants/<int:tid>")
def detail(tid: int):
    """A tenant with a home goes to their account page; otherwise show their details."""
    conn = db()
    tenant = tenants.get_tenant(conn, tid)
    leases = tenants.tenant_leases(conn, tid)
    if leases:
        current = [l for l in leases if l["status"] in ("active", "month_to_month", "future")]
        return redirect(url_for("leases.detail", lease_id=(current or leases)[0]["id"]))
    return render_template("tenants/detail.html", tenant=tenant)


@bp.route("/tenants/<int:tid>/edit", methods=["GET", "POST"])
def edit(tid: int):
    tenant = tenants.get_tenant(db(), tid)
    back = request.values.get("back") or url_for("tenants.detail", tid=tid)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Saved") as r:
            fields = person_fields(f)
            f.check()
            tenants.save_tenant(db(), tid, fields)
        if r["done"]:
            return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("tenants.index"))
        values = request.form
    else:
        values = values_from(tenant)
        values["name"] = f"{tenant['first_name']} {tenant['last_name']}".strip()
    return render_template("tenants/form.html", tenant=tenant, values=values, back=back)
