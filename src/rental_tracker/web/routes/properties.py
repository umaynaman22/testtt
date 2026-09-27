"""Properties, units and owners."""
from __future__ import annotations

import csv
import io

from flask import Blueprint, Response, redirect, render_template, request, url_for

from ...domain.money import format_money
from ...services import documents, expenses, ledger, portfolio, reports
from ...services.common import csv_row
from .. import attempt, db, today
from ..forms import Form, values_from
from . import options

bp = Blueprint("properties", __name__)

PROPERTY_MONEY = {"purchase_price": "purchase_price_cents", "land_value": "land_value_cents",
                  "cash_invested": "cash_invested_cents", "estimated_value": "estimated_value_cents"}


def _property_fields(f: Form) -> dict:
    return {
        "owner_id": f.id("owner_id"), "code": f.str("code", "Code", required=True),
        "name": f.str("name", "Name", required=True),
        "property_type": f.choice("property_type", "Property type", portfolio.PROPERTY_TYPES),
        "address_line1": f.str("address_line1", "Address", required=True), "address_line2": f.str("address_line2"),
        "city": f.str("city", "City", required=True), "state": f.str("state", "State", required=True),
        "postal_code": f.str("postal_code", "ZIP", required=True), "parcel_number": f.str("parcel_number"),
        "year_built": f.int("year_built", "Year built", lo=1600, hi=2200), "hoa_name": f.str("hoa_name"),
        "purchase_date": f.date("purchase_date", "Purchase date"),
        "purchase_price_cents": f.money("purchase_price", "Purchase price"),
        "land_value_cents": f.money("land_value", "Land value"),
        "placed_in_service_date": f.date("placed_in_service_date", "Placed in service"),
        "cash_invested_cents": f.money("cash_invested", "Cash invested"),
        "estimated_value_cents": f.money("estimated_value", "Estimated value"),
        "value_as_of": f.date("value_as_of", "Value as of"),
        "status": f.choice("status", "Status", portfolio.PROPERTY_STATUSES, "active"),
        "sold_date": f.date("sold_date", "Sold date"), "notes": f.str("notes"),
    }


def _form_context(values):
    return {"values": values, "owner_options": [(o["id"], o["name"]) for o in portfolio.list_owners(db())],
            "types": options(portfolio.PROPERTY_TYPES), "statuses": options(portfolio.PROPERTY_STATUSES)}


@bp.route("/properties")
def index():
    a = request.args
    rows = portfolio.list_properties(db(), q=a.get("q", ""), tag_id=a.get("tag", type=int),
                                     owner_id=a.get("owner", type=int), status=a.get("status", "active"),
                                     city=a.get("city", ""), vacant_only=a.get("vacant") == "1",
                                     sort=a.get("sort", "code"))
    if a.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["code", "name", "owner", "address", "city", "state", "units", "occupied", "rent", "balance", "tags"])
        for r in rows:
            w.writerow(csv_row([r["code"], r["name"], r["owner_name"], r["address_line1"], r["city"], r["state"],
                                r["units"], r["occupied"], format_money(r["rent_cents"]),
                                format_money(r["balance_cents"]), r["tags"] or ""]))
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=properties.csv"})
    totals = {"units": sum(r["units"] for r in rows), "occupied": sum(r["occupied"] for r in rows),
              "rent": sum(r["rent_cents"] for r in rows), "balance": sum(r["balance_cents"] for r in rows)}
    return render_template("properties/index.html", rows=rows, totals=totals, tags=portfolio.all_tags(db()),
                           owners=portfolio.list_owners(db()), cities=portfolio.cities(db()))


@bp.route("/properties/new", methods=["GET", "POST"])
def new():
    if not portfolio.list_owners(db()):
        return redirect(url_for("properties.owners", first=1))
    values = request.form if request.method == "POST" else {"status": "active", "property_type": "single_family"}
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Property created") as r:
            fields = _property_fields(f)
            labels = [x.strip() for x in f.raw("units").split(",") if x.strip()] or ["Main"]
            f.check()
            r["id"] = portfolio.save_property(db(), None, fields, tags=f.raw("tags").split(","), unit_labels=labels)
        if r["done"]:
            return redirect(url_for("properties.detail", pid=r["id"]))
    return render_template("properties/form.html", prop=None, **_form_context(values))


@bp.route("/properties/<int:pid>/edit", methods=["GET", "POST"])
def edit(pid: int):
    prop = portfolio.get_property(db(), pid)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Property saved") as r:
            fields = _property_fields(f)
            f.check()
            portfolio.save_property(db(), pid, fields, tags=f.raw("tags").split(","))
        if r["done"]:
            return redirect(url_for("properties.detail", pid=pid))
        values = request.form
    else:
        values = values_from(prop, PROPERTY_MONEY)
        values["tags"] = ", ".join(portfolio.property_tags(db(), pid))
    return render_template("properties/form.html", prop=prop, **_form_context(values))


@bp.route("/properties/<int:pid>")
def detail(pid: int):
    conn = db()
    prop = portfolio.get_property(conn, pid)
    year_start = f"{today().year}-01-01"
    pl = reports.income_statement(conn, year_start, today().isoformat(), [pid])
    lines = {row["line"]: row.get("amount") for row in pl.rows}
    return render_template(
        "properties/detail.html", prop=prop, units=portfolio.units_for_property(conn, pid),
        tags=portfolio.property_tags(conn, pid), docs=documents.for_entity(conn, "property", pid),
        ytd={"income": lines.get("Total income", 0), "expenses": lines.get("Total operating expenses", 0),
             "noi": lines.get("Net operating income (NOI)", 0)},
        recent_payments=ledger.list_payments(conn, property_ids=[pid], limit=10),
        recent_expenses=expenses.list_expenses(conn, property_ids=[pid], limit=10),
        values={}, unit_statuses=options(portfolio.UNIT_STATUSES))


@bp.route("/properties/<int:pid>/units", methods=["POST"])
def add_unit(pid: int):
    f = Form(request.form)
    with attempt("Unit added"):
        fields = {"unit_label": f.str("unit_label", "Unit label", required=True),
                  "bedrooms": f.num("bedrooms", "Bedrooms"), "bathrooms": f.num("bathrooms", "Bathrooms"),
                  "square_feet": f.int("square_feet", "Square feet", lo=0, hi=10**6),
                  "market_rent_cents": f.money("market_rent", "Market rent")}
        f.check()
        portfolio.save_unit(db(), None, pid, fields)
    return redirect(url_for("properties.detail", pid=pid) + "#units")


@bp.route("/units/<int:unit_id>", methods=["GET", "POST"])
def unit_detail(unit_id: int):
    conn = db()
    unit = portfolio.get_unit(conn, unit_id)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Unit saved") as r:
            fields = {"unit_label": f.str("unit_label", "Unit label", required=True),
                      "bedrooms": f.num("bedrooms", "Bedrooms"), "bathrooms": f.num("bathrooms", "Bathrooms"),
                      "square_feet": f.int("square_feet", "Square feet", lo=0, hi=10**6),
                      "market_rent_cents": f.money("market_rent", "Market rent"),
                      "status": f.choice("status", "Status", portfolio.UNIT_STATUSES), "notes": f.str("notes")}
            f.check()
            portfolio.save_unit(conn, unit_id, unit["property_id"], fields)
        if r["done"]:
            return redirect(url_for("properties.unit_detail", unit_id=unit_id))
        values = request.form
    else:
        values = values_from(unit, {"market_rent": "market_rent_cents"})
    return render_template("properties/unit.html", unit=unit, leases=portfolio.unit_leases(conn, unit_id),
                           docs=documents.for_entity(conn, "unit", unit_id), values=values,
                           statuses=options(portfolio.UNIT_STATUSES))


# ---- owners ------------------------------------------------------------------

def _owner_fields(f: Form) -> dict:
    return {"name": f.str("name", "Name", required=True),
            "entity_type": f.choice("entity_type", "Entity type", portfolio.ENTITY_TYPES, "individual"),
            "tax_id_last4": f.str("tax_id_last4", max_len=4), "email": f.str("email"), "phone": f.str("phone"),
            "mailing_address": f.str("mailing_address"),
            "management_fee_bp": f.percent_bp("management_fee", "Management fee"), "notes": f.str("notes")}


@bp.route("/owners", methods=["GET", "POST"])
def owners():
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Owner added") as r:
            fields = _owner_fields(f)
            f.check()
            r["id"] = portfolio.save_owner(db(), None, fields)
        if r["done"]:
            if request.args.get("first"):
                return redirect(url_for("properties.new"))
            return redirect(url_for("properties.owners"))
    return render_template("properties/owners.html", owners=portfolio.list_owners(db()),
                           values=request.form if request.method == "POST" else {"entity_type": "llc"},
                           entity_types=options(portfolio.ENTITY_TYPES, {"llc": "LLC"}),
                           first=request.args.get("first"))


@bp.route("/owners/<int:owner_id>", methods=["GET", "POST"])
def owner_edit(owner_id: int):
    owner = portfolio.get_owner(db(), owner_id)
    if request.method == "POST":
        f = Form(request.form)
        with attempt("Owner saved") as r:
            fields = _owner_fields(f)
            f.check()
            portfolio.save_owner(db(), owner_id, fields)
        if r["done"]:
            return redirect(url_for("properties.owners"))
        values = request.form
    else:
        values = values_from(owner, percent={"management_fee": "management_fee_bp"})
    props = portfolio.list_properties(db(), owner_id=owner_id, status="all")
    return render_template("properties/owner_edit.html", owner=owner, values=values, props=props,
                           entity_types=options(portfolio.ENTITY_TYPES, {"llc": "LLC"}))
