"""Tenants."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ...services import documents, tenants
from .. import attempt, db
from ..forms import Form, values_from

bp = Blueprint("tenants", __name__)


def _fields(f: Form) -> dict:
    return {"first_name": f.str("first_name", "First name", required=True),
            "last_name": f.str("last_name", "Last name", required=True),
            "email": f.str("email"), "phone": f.str("phone"), "alt_phone": f.str("alt_phone"),
            "emergency_contact_name": f.str("emergency_contact_name"),
            "emergency_contact_phone": f.str("emergency_contact_phone"),
            "forwarding_address": f.str("forwarding_address"), "external_ref": f.str("external_ref"),
            "notes": f.str("notes")}


@bp.route("/tenants")
def index():
    status = request.args.get("status", "current")
    rows = tenants.list_tenants(db(), q=request.args.get("q", ""), status=status)
    return render_template("tenants/index.html", rows=rows, status=status)


@bp.route("/tenants/new", methods=["GET", "POST"])
def new():
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Tenant added") as r:
            fields = _fields(f)
            f.check()
            r["id"] = tenants.save_tenant(db(), None, fields)
        if r["done"]:
            return redirect(url_for("tenants.detail", tid=r["id"]))
    return render_template("tenants/form.html", tenant=None, values=request.form)


@bp.route("/tenants/<int:tid>")
def detail(tid: int):
    conn = db()
    return render_template("tenants/detail.html", tenant=tenants.get_tenant(conn, tid),
                           leases=tenants.tenant_leases(conn, tid), docs=documents.for_entity(conn, "tenant", tid))


@bp.route("/tenants/<int:tid>/edit", methods=["GET", "POST"])
def edit(tid: int):
    tenant = tenants.get_tenant(db(), tid)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Tenant saved") as r:
            fields = _fields(f)
            f.check()
            tenants.save_tenant(db(), tid, fields)
        if r["done"]:
            return redirect(url_for("tenants.detail", tid=tid))
        values = request.form
    else:
        values = values_from(tenant)
    return render_template("tenants/form.html", tenant=tenant, values=values)
