"""Units: the flat list of places you rent out.

Behind the scenes each unit is a property with a single unit (see
portfolio.flatten_units), so these routes keep their /properties URLs.
"""
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ...services import ledger, portfolio, tenants
from .. import attempt, db, today
from ..forms import Form, values_from

bp = Blueprint("properties", __name__)


def _fields(f: Form) -> dict:
    return {"name": f.str("name"), "city": f.str("city"), "state": f.str("state"),
            "postal_code": f.str("postal_code"), "notes": f.str("notes")}


def _current_tenancies(conn, property_id: int | None = None) -> dict[int, list[dict]]:
    """Current (and upcoming) tenancies by unit, people living there now first."""
    by_prop: dict[int, list[dict]] = {}
    for r in tenants.tenancies(conn, today=today(), status="current", property_id=property_id):
        by_prop.setdefault(r["property_id"], []).append(r)
    for rows in by_prop.values():
        rows.sort(key=lambda r: r["status"] == "future")
    return by_prop


@bp.route("/properties")
def index():
    a = request.args
    conn = db()
    rows = portfolio.list_properties(conn, q=a.get("q", ""), status="active", sort=a.get("sort", "name"))
    people = {pid: rows_[0] for pid, rows_ in _current_tenancies(conn).items()}
    totals = {"rent": sum(r["rent_cents"] for r in rows), "balance": sum(r["balance_cents"] for r in rows)}
    return render_template("properties/index.html", rows=rows, people=people, totals=totals)


@bp.route("/properties/new", methods=["GET", "POST"])
def new():
    values = request.form if request.method == "POST" else {}
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Unit added") as r:
            fields = _fields(f)
            f.check()
            r["id"] = portfolio.save_property(db(), None, fields)
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
    unit = conn.execute("SELECT MIN(id) FROM units WHERE property_id = ?", (pid,)).fetchone()[0]
    return render_template("properties/detail.html", prop=prop, unit_id=unit,
                           tenancies=_current_tenancies(conn, pid).get(pid, []),
                           payments=ledger.list_payments(conn, property_ids=[pid], include_voided=False, limit=10),
                           past=tenants.tenancies(conn, today=today(), status="past", property_id=pid))


@bp.route("/units/<int:unit_id>")
def unit_detail(unit_id: int):
    """Old links to a unit go to its page."""
    return redirect(url_for("properties.detail", pid=portfolio.get_unit(db(), unit_id)["property_id"]))
