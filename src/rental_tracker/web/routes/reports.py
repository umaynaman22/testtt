"""Reports: pick one, filter by unit, view, print or export."""
from __future__ import annotations

from flask import Blueprint, Response, abort, render_template, request

from ...domain.periods import parse_period, period_of
from ...services import portfolio, reports
from .. import db, today

bp = Blueprint("reports", __name__)


@bp.route("/reports")
def index():
    return render_template("reports/index.html", reports=reports.REPORTS)


@bp.route("/reports/<key>")
def view(key: str):
    if key not in reports.REPORTS:
        abort(404)
    conn = db()
    t = today()
    pid = request.args.get("property", type=int)
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
    if request.args.get("format") == "csv":
        return Response(rep.to_csv(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={key}-{t.isoformat()}.csv"})
    return render_template("reports/view.html", rep=rep, key=key, period=period,
                           props=[(p["id"], p["name"]) for p in portfolio.list_properties(conn, status="all", sort="name")])
