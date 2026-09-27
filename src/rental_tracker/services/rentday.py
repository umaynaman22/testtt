"""The Rent Day grid: every current lease with what it owes this month (BLUEPRINT §9)."""
from __future__ import annotations

import sqlite3

from ..domain.periods import period_end, period_start
from . import ledger


def rows(conn: sqlite3.Connection, period: str, *, q: str = "", show: str = "all",
         lease_id: int | None = None) -> list[dict]:
    start, end = period_start(period).isoformat(), period_end(period).isoformat()
    where, params = ["l.status IN ('active','month_to_month')"], [period, start, end]
    if lease_id:
        where = ["l.id = ?"]
        params.append(lease_id)
    if q:
        where.append("(p.code LIKE ? OR p.name LIKE ? OR u.unit_label LIKE ? OR EXISTS ("
                     "SELECT 1 FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id "
                     "WHERE lt.lease_id = l.id AND t.first_name || ' ' || t.last_name LIKE ?))")
        params += [f"%{q}%"] * 4
    out = []
    status = None
    for r in conn.execute(f"""
        SELECT l.id AS lease_id, p.id AS property_id, p.code AS property_code, u.unit_label,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants,
               COALESCE((SELECT SUM(c.amount_cents) FROM charges c WHERE c.lease_id = l.id AND c.period = ?
                          AND c.source = 'auto' AND c.charge_type <> 'late_fee' AND c.voided_at IS NULL), 0) AS billed_cents,
               COALESCE((SELECT SUM(pay.amount_cents) FROM payments pay WHERE pay.lease_id = l.id
                          AND pay.received_date BETWEEN ? AND ? AND pay.voided_at IS NULL), 0) AS paid_cents,
               b.balance_cents, cr.current_rent_cents,
               (SELECT pay.method FROM payments pay WHERE pay.lease_id = l.id AND pay.voided_at IS NULL
                 ORDER BY pay.received_date DESC, pay.id DESC LIMIT 1) AS last_method,
               (SELECT pay.method_other FROM payments pay WHERE pay.lease_id = l.id AND pay.voided_at IS NULL
                 ORDER BY pay.received_date DESC, pay.id DESC LIMIT 1) AS last_method_other
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN v_lease_balances b ON b.lease_id = l.id
          JOIN v_lease_current_rent cr ON cr.lease_id = l.id
         WHERE {" AND ".join(where)}
         ORDER BY p.code, u.unit_label""", params):
        row = dict(r)
        if status is None:
            status = ledger.period_status(conn, period, [lease_id] if lease_id else None)
        billed, unpaid = status.get(row["lease_id"], (0, 0))
        row["period_paid_cents"] = billed - unpaid
        if row["balance_cents"] <= 0:
            row["state"] = "paid"
        elif row["period_paid_cents"] > 0:
            row["state"] = "partial"
        else:
            row["state"] = "unpaid"
        if show == "unpaid" and row["state"] == "paid":
            continue
        if show == "paid" and row["state"] != "paid":
            continue
        out.append(row)
    return out
