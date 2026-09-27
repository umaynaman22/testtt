"""Properties and their units."""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ...services import ledger, portfolio, tenants
from .. import attempt, db, today
from ..forms import Form, values_from

bp = Blueprint("properties", __name__)


def _fields(f: Form) -> dict:
    return {"name": f.str("name"), "city": f.str("city"), "state": f.str("state"),
            "postal_code": f.str("postal_code"), "notes": f.str("notes")}


@bp.route("/properties")
def index():
    a = request.args
    rows = portfolio.list_properties(db(), q=a.get("q", ""), status="active",
                                     vacant_only=a.get("vacant") == "1", sort=a.get("sort", "name"))
    totals = {"units": sum(r["units"] for r in rows), "occupied": sum(r["occupied"] for r in rows),
              "rent": sum(r["rent_cents"] for r in rows), "balance": sum(r["balance_cents"] for r in rows)}
    return render_template("properties/index.html", rows=rows, totals=totals)


@bp.route("/properties/new", methods=["GET", "POST"])
def new():
    values = request.form if request.method == "POST" else {}
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Property added") as r:
            fields = _fields(f)
            f.check()
            r["id"] = portfolio.save_property(db(), None, fields,
                                              unit_labels=portfolio.parse_unit_labels(f.raw("units")))
        if r["done"]:
            return redirect(url_for("properties.detail", pid=r["id"]))
    return render_template("properties/form.html", prop=None, values=values)


@bp.route("/properties/<int:pid>/edit", methods=["GET", "POST"])
def edit(pid: int):
    prop = portfolio.get_property(db(), pid)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Saved") as r:
            fields = _fields(f)
            f.check()
            portfolio.save_property(db(), pid, fields)
        if r["done"]:
            return redirect(url_for("properties.detail", pid=pid))
        values = request.form
    else:
        values = values_from(prop)
    return render_template("properties/form.html", prop=prop, values=values)


@bp.route("/properties/<int:pid>")
def detail(pid: int):
    conn = db()
    prop = portfolio.get_property(conn, pid)
    units = portfolio.units_for_property(conn, pid)
    people = {r["unit_id"]: r for r in tenants.tenancies(conn, today=today(), status="current", property_id=pid)}
    return render_template("properties/detail.html", prop=prop, units=units, people=people,
                           payments=ledger.list_payments(conn, property_ids=[pid], include_voided=False, limit=10),
                           past=tenants.tenancies(conn, today=today(), status="past", property_id=pid))


@bp.route("/properties/<int:pid>/units", methods=["POST"])
def add_unit(pid: int):
    f = Form(request.form)
    with attempt("Unit added"):
        portfolio.save_unit(db(), None, pid, {"unit_label": f.str("unit_label")})
    return redirect(url_for("properties.detail", pid=pid) + "#units")


@bp.route("/units/<int:unit_id>", methods=["GET", "POST"])
def unit_detail(unit_id: int):
    conn = db()
    unit = portfolio.get_unit(conn, unit_id)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Saved") as r:
            portfolio.save_unit(conn, unit_id, unit["property_id"],
                                {"unit_label": f.str("unit_label"), "notes": f.str("notes")})
        if r["done"]:
            return redirect(url_for("properties.detail", pid=unit["property_id"]))
        values = request.form
    else:
        values = values_from(unit)
    return render_template("properties/unit.html", unit=unit, leases=portfolio.unit_leases(conn, unit_id),
                           values=values)
