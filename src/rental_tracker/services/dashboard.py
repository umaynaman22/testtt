"""Numbers for the dashboard: collected, owed and who's late."""
from __future__ import annotations

import sqlite3
from datetime import date

from ..domain.periods import period_of, period_start
from . import late_fees, ledger, reports, tenants


def build(conn: sqlite3.Connection, today: date) -> dict:
    period = period_of(today)
    coll = reports.collections(conn, period)
    totals = coll.totals or {}
    billed, paid = totals.get("billed_cents", 0), totals.get("paid_cents", 0)
    rows = tenants.tenancies(conn, today=today, status="all")
    late = sorted((r for r in rows if r["past_due_cents"] > 0), key=lambda r: (-r["days_late"], -r["past_due_cents"]))
    return {
        "period_start": period_start(period),
        "billed": billed, "paid": paid, "received": totals.get("received_cents", 0),
        "paid_pct": 100 * paid / billed if billed else None,
        "owed": sum(max(r["balance_cents"], 0) for r in rows),
        "late": late, "late_total": sum(r["past_due_cents"] for r in late),
        "recent_payments": ledger.list_payments(conn, include_voided=False, limit=8),
        "late_fee_count": len(late_fees.find_candidates(conn, today)),
        "property_count": conn.execute("SELECT COUNT(*) FROM properties WHERE status = 'active'").fetchone()[0],
    }
