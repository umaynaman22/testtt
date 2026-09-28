"""Reports: pick one, filter by unit, view, print or export."""
from __future__ import annotations

from flask import Blueprint, abort, render_template, request

from ...domain.periods import parse_period, period_of
from ...services import excel, portfolio, reports
from .. import db, today
from ..forms import record_id
from . import excel_download

bp = Blueprint("reports", __name__)


@bp.route("/reports")
def index():
    return render_template("reports/index.html", reports=reports.REPORTS)


@bp.route("/reports/everything.xlsx")
def everything():
    """Every unit, tenant, payment and history line in one Excel workbook."""
    return excel_download(excel.everything(db(), today()), f"rental-tracker-{today().isoformat()}")


@bp.route("/reports/<key>")
def view(key: str):
    if key not in reports.REPORTS:
        abort(404)
    conn = db()
    t = today()
    pid = record_id(request.args.get("property"))
    pids = [pid] if pid else None
    period = request.args.get("period") or period_of(t)
    try:
        parse_period(period)
    except ValueError:
        period = period_of(t)
    rep = {
        "rent-roll": lambda: reports.rent_roll(conn, pids),
        "aging": lambda: reports.aging_report(conn, t, pids),
        "collections": lambda: reports.collections(conn, period, pids),
    }[key]()
    if request.args.get("format") == "xlsx":
        return excel_download(excel.workbook(rep.to_sheet()), f"{key}-{t.isoformat()}")
    return render_template("reports/view.html", rep=rep, key=key, period=period,
                           props=[(p["id"], p["name"]) for p in portfolio.list_properties(conn, status="all", sort="name")])
