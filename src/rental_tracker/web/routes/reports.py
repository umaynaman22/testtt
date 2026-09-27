"""Reports: pick one, filter it, view or export CSV."""
from __future__ import annotations

from flask import Blueprint, Response, abort, render_template, request

from ...domain.periods import parse_period, period_of
from ...services import expenses, portfolio, reports
from .. import db, today

bp = Blueprint("reports", __name__)

# which filters each report shows
FILTERS = {
    "rent-roll": {"scope"}, "aging": {"scope"}, "collections": {"scope", "period"},
    "income-statement": {"scope", "range", "basis"}, "schedule-e": {"scope", "year"},
    "expenses": {"scope", "range", "category"}, "vacancy": {"scope"}, "expirations": {"scope", "months"},
    "deposits": {"scope"}, "rent-vs-market": {"scope"}, "performance": {"scope", "range"}, "1099": {"year"},
}


@bp.route("/reports")
def index():
    return render_template("reports/index.html", reports=reports.REPORTS)


@bp.route("/reports/<key>")
def view(key: str):
    if key not in reports.REPORTS:
        abort(404)
    conn = db()
    a = request.args
    t = today()
    pids = portfolio.property_ids_for(conn, property_id=a.get("property", type=int), tag_id=a.get("tag", type=int),
                                      owner_id=a.get("owner", type=int))
    start = a.get("start") or f"{t.year}-01-01"
    end = a.get("end") or t.isoformat()
    year = a.get("year", type=int) or (t.year - 1 if t.month <= 3 else t.year)
    period = a.get("period") or period_of(t)
    try:
        parse_period(period)
    except ValueError:
        period = period_of(t)
    if key == "rent-roll":
        rep = reports.rent_roll(conn, pids)
    elif key == "aging":
        rep = reports.aging_report(conn, t, pids)
    elif key == "collections":
        rep = reports.collections(conn, period, pids)
    elif key == "income-statement":
        rep = reports.income_statement(conn, start, end, pids, a.get("basis", "cash"))
    elif key == "schedule-e":
        rep = reports.schedule_e(conn, year, pids)
    elif key == "expenses":
        rep = reports.expense_detail(conn, start, end, pids, a.get("category", type=int))
    elif key == "vacancy":
        rep = reports.vacancy(conn, t, pids)
    elif key == "expirations":
        rep = reports.lease_expirations(conn, t, a.get("months", type=int) or 12, pids)
    elif key == "deposits":
        rep = reports.deposit_register(conn, t, pids)
    elif key == "rent-vs-market":
        rep = reports.rent_vs_market(conn, pids)
    elif key == "performance":
        rep = reports.property_performance(conn, start, end, pids)
    else:
        rep = reports.vendor_1099(conn, year)
    if a.get("format") == "csv":
        return Response(rep.to_csv(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={key}-{t.isoformat()}.csv"})
    return render_template("reports/view.html", rep=rep, key=key, filters=FILTERS[key], start=start, end=end,
                           year=year, period=period,
                           props=[(p["id"], p["code"]) for p in portfolio.list_properties(conn, status="all")],
                           tags=portfolio.all_tags(conn), owners=portfolio.list_owners(conn),
                           cats=expenses.list_categories(conn))
