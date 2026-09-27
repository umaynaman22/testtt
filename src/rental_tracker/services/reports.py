"""Reports (BLUEPRINT §10). Each returns a Report that the UI renders and exports to CSV."""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..domain.allocation import BUCKETS, aging
from ..domain.periods import period_end, period_start
from . import ledger
from .common import csv_row


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = "text"  # text | money | date | pct | int
    link: tuple[str, str] | None = None  # (entity type, id key in row)


@dataclass
class Report:
    key: str
    title: str
    subtitle: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    totals: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c.label for c in self.columns])
        for row in self.rows + ([self.totals] if self.totals else []):
            w.writerow(csv_row([_csv_value(row.get(c.key), c.kind) for c in self.columns]))
        return buf.getvalue()


def _csv_value(v: Any, kind: str) -> Any:
    if v is None:
        return ""
    if kind == "money":
        sign = "-" if v < 0 else ""
        return f"{sign}{abs(v) // 100}.{abs(v) % 100:02d}"
    return v


def _in(col: str, ids: list[int] | None) -> tuple[str, list[int]]:
    if ids is None:
        return "1", []
    if not ids:
        return "0", []
    return f"{col} IN ({','.join('?' * len(ids))})", list(ids)


def _sum_totals(rows: list[dict], columns: list[Column], label_key: str, label: str = "Total") -> dict:
    totals: dict[str, Any] = {label_key: label, "_class": "total"}
    for c in columns:
        if c.kind == "money" and c.key != label_key:
            totals[c.key] = sum(r.get(c.key) or 0 for r in rows)
    return totals


TENANTS_SQL = """(SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                    FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                   WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant'))"""


# ---- 1. Rent roll ----------------------------------------------------------------

def rent_roll(conn: sqlite3.Connection, property_ids: list[int] | None = None) -> Report:
    flt, params = _in("property_id", property_ids)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM v_rent_roll WHERE occupancy = 'occupied' AND {flt} ORDER BY property_code, unit_label",
        params)]
    cols = [Column("property_code", "Property", link=("property", "property_id")),
            Column("unit_label", "Unit", link=("unit", "unit_id")),
            Column("tenants", "Tenants", link=("lease", "lease_id")),
            Column("lease_status", "Status"), Column("start_date", "Moved in", "date"),
            Column("current_rent_cents", "Rent", "money"),
            Column("balance_cents", "Balance", "money")]
    for r in rows:
        r["lease_status"] = (r["lease_status"] or "").replace("_", " ")
    return Report("rent-roll", "Rent roll", f"{len(rows)} tenants", cols, rows, _sum_totals(rows, cols, "property_code"))


# ---- 2. Delinquency / aging --------------------------------------------------------

def aging_report(conn: sqlite3.Connection, today: date, property_ids: list[int] | None = None,
                 include_credits: bool = False) -> Report:
    flt, params = _in("u.property_id", property_ids)
    leases = conn.execute(f"""
        SELECT l.id AS lease_id, l.status, p.code AS property_code, p.id AS property_id, u.unit_label,
               {TENANTS_SQL} AS tenants, b.balance_cents
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN v_lease_balances b ON b.lease_id = l.id
         WHERE b.balance_cents <> 0 AND {flt}
         ORDER BY p.code, u.unit_label""", params).fetchall()
    ledgers = ledger.load_ledgers(conn, [r["lease_id"] for r in leases])
    order = ledger.payment_order(conn)
    rows = []
    for r in leases:
        charges, payments = ledgers.get(r["lease_id"], ([], []))
        ag = aging(charges, payments, today, order)
        if not include_credits and ag["past_due"] == 0 and ag["current"] == 0:
            continue
        rows.append({**dict(r), **{f"b_{k}": ag[k] for k in BUCKETS}, "credit": ag["credit"],
                     "past_due": ag["past_due"]})
    rows.sort(key=lambda x: -x["past_due"])
    cols = [Column("property_code", "Property", link=("property", "property_id")),
            Column("unit_label", "Unit"), Column("tenants", "Tenants", link=("lease", "lease_id")),
            Column("status", "Lease"),
            Column("b_current", "Not yet due", "money"), Column("b_1_30", "1–30 days", "money"),
            Column("b_31_60", "31–60", "money"), Column("b_61_90", "61–90", "money"),
            Column("b_90_plus", "90+", "money"), Column("past_due", "Past due", "money"),
            Column("balance_cents", "Balance", "money")]
    return Report("aging", "Who owes money", f"As of {today.isoformat()} · {len(rows)} leases owing",
                  cols, rows, _sum_totals(rows, cols, "property_code"),
                  ["Payments pay off the oldest charges first, so the columns show how long each "
                   "unpaid amount has been waiting."])


# ---- 3. Monthly collections ----------------------------------------------------------

def collections(conn: sqlite3.Connection, period: str, property_ids: list[int] | None = None) -> Report:
    start, end = period_start(period).isoformat(), period_end(period).isoformat()
    flt, params = _in("p.id", property_ids)
    props = {r["id"]: {"property_id": r["id"], "property_code": r["code"], "property_name": r["name"],
                       "billed_cents": 0, "paid_cents": 0, "received_cents": 0, "outstanding_cents": 0}
             for r in conn.execute(f"SELECT id, code, name FROM properties p WHERE p.status = 'active' AND {flt} "
                                   "ORDER BY code", params)}
    lease_prop = {r[0]: r[1] for r in conn.execute(
        "SELECT l.id, u.property_id FROM leases l JOIN units u ON u.id = l.unit_id")}
    for lid, (billed, unpaid) in ledger.period_status(conn, period).items():
        row = props.get(lease_prop.get(lid))
        if row:
            row["billed_cents"] += billed
            row["paid_cents"] += billed - unpaid
    for r in conn.execute("""
        SELECT u.property_id, SUM(pay.amount_cents) FROM payments pay JOIN leases l ON l.id = pay.lease_id
          JOIN units u ON u.id = l.unit_id
         WHERE pay.received_date BETWEEN ? AND ? AND pay.voided_at IS NULL AND pay.method <> 'deposit_applied'
         GROUP BY u.property_id""", (start, end)):
        if r[0] in props:
            props[r[0]]["received_cents"] = r[1]
    for r in conn.execute("""
        SELECT u.property_id, SUM(b.balance_cents) FROM leases l JOIN units u ON u.id = l.unit_id
          JOIN v_lease_balances b ON b.lease_id = l.id WHERE b.balance_cents > 0 GROUP BY u.property_id"""):
        if r[0] in props:
            props[r[0]]["outstanding_cents"] = r[1]
    rows = [r for r in props.values() if r["billed_cents"] or r["received_cents"] or r["outstanding_cents"]]
    for r in rows:
        r["rate"] = (100 * r["paid_cents"] / r["billed_cents"]) if r["billed_cents"] else None
    cols = [Column("property_code", "Property", link=("property", "property_id")),
            Column("property_name", "Name"), Column("billed_cents", "Billed for the month", "money"),
            Column("paid_cents", "Paid of that", "money"), Column("rate", "Collected %", "pct"),
            Column("received_cents", "Cash received in month", "money"),
            Column("outstanding_cents", "Total owed now", "money")]
    totals = _sum_totals(rows, cols, "property_code")
    totals["rate"] = (100 * totals["paid_cents"] / totals["billed_cents"]) if totals["billed_cents"] else None
    return Report("collections", "Monthly collections", f"{period} · rent and recurring charges billed for the month",
                  cols, rows, totals,
                  ["'Paid of that' counts rent paid early (before the month began) as paid. 'Cash received' is "
                   "money that arrived during the month, including payments toward older balances."])


REPORTS = {
    "rent-roll": ("Rent roll", "Every tenant: where they live, rent, balance"),
    "aging": ("Who owes money", "Unpaid amounts by how many days late"),
    "collections": ("Monthly collections", "Rent billed vs paid, per property"),
}
