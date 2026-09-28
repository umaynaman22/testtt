"""Tenants: the list, and people who aren't linked to a unit yet."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ...services import excel, tenants
from .. import attempt, db, today
from ..forms import Form, values_from
from . import excel_download, safe_next

bp = Blueprint("tenants", __name__)


def person_fields(f: Form) -> dict:
    first, last = tenants.split_name(f.raw("name"))
    return {"first_name": first, "last_name": last, "phone": f.str("phone"), "notes": f.str("notes")}


@bp.route("/tenants")
def index():
    rows = tenants.tenancies(db(), today=today(), status="all", q=request.args.get("q", ""))
    if request.args.get("format") == "xlsx":
        return excel_download(excel.workbook(excel.tenants_sheet(rows)), f"tenants-{today().isoformat()}")
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
    back = safe_next(request.values.get("back"), url_for("tenants.detail", tid=tid))
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Saved") as r:
            fields = person_fields(f)
            f.check()
            tenants.save_tenant(db(), tid, fields)
        if r["done"]:
            return redirect(back)
        values = request.form
    else:
        values = values_from(tenant)
        values["name"] = f"{tenant['first_name']} {tenant['last_name']}".strip()
    return render_template("tenants/form.html", tenant=tenant, values=values, back=back)
