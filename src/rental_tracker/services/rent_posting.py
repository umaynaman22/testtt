"""Automatic rent and add-on billing, run as a catch-up job (BLUEPRINT §7.3).

Safe to run any number of times: the unique index ux_charges_auto_period
means each lease/charge/month is billed exactly once.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..domain.periods import parse_date
from ..domain.rent import plan_charges
from .common import audit, books_locked_through, get_int_setting, get_setting

LABELS = {"pet_rent": "Pet rent", "parking": "Parking", "storage": "Storage", "utility": "Utilities",
          "other": "Recurring charge"}


@dataclass
class PostingResult:
    posted: int = 0
    amount_cents: int = 0
    skipped_locked: int = 0


def post_rent(conn: sqlite3.Connection, today: date, lease_ids: list[int] | None = None) -> PostingResult:
    lookahead = get_int_setting(conn, "rent_post_days_before_due", 0)
    method = get_setting(conn, "proration_method", "actual_days")
    through = today + timedelta(days=max(lookahead, 0))
    lock = books_locked_through(conn)
    result = PostingResult()
    sql = "SELECT * FROM leases WHERE status IN ('active','month_to_month')"
    params: tuple = ()
    if lease_ids is not None:
        sql += f" AND id IN ({','.join('?' * len(lease_ids)) or 'NULL'})"
        params = tuple(lease_ids)
    for lease in conn.execute(sql, params).fetchall():
        start = parse_date(lease["start_date"])
        billing_start = parse_date(lease["billing_start_date"]) if lease["billing_start_date"] else start
        end = parse_date(lease["end_date"]) if lease["status"] == "active" and lease["end_date"] else None
        changes = [(parse_date(r["effective_date"]), r["rent_cents"]) for r in conn.execute(
            "SELECT effective_date, rent_cents FROM lease_rent_changes WHERE lease_id = ?", (lease["id"],))]
        last = conn.execute("SELECT MAX(period) FROM charges WHERE lease_id = ? AND source = 'auto' "
                            "AND charge_type = 'rent'", (lease["id"],)).fetchone()[0]
        from_period = max(last or "", billing_start.isoformat()[:7]) or None
        for plan in plan_charges(start=start, end=end, due_day=lease["rent_due_day"],
                                 base_cents=lease["rent_cents"], changes=changes,
                                 prorate_partial=bool(lease["prorate_partial_months"]), method=method,
                                 from_period=from_period, through=through):
            desc = f"Rent {plan.period}"
            if plan.prorated_days:
                desc += f" (prorated, {plan.prorated_days} days)"
            _insert(conn, result, lock, lease["id"], "rent", None, plan, desc)
        for rc in conn.execute("SELECT * FROM lease_recurring_charges WHERE lease_id = ?", (lease["id"],)).fetchall():
            rc_start = max(start, parse_date(rc["start_date"]))
            rc_end_candidates = [d for d in (end, parse_date(rc["end_date"]) if rc["end_date"] else None) if d]
            rc_end = min(rc_end_candidates) if rc_end_candidates else None
            if rc_end and rc_end < rc_start:
                continue
            last_rc = conn.execute("SELECT MAX(period) FROM charges WHERE lease_id = ? AND source = 'auto' "
                                   "AND recurring_charge_id = ?", (lease["id"], rc["id"])).fetchone()[0]
            rc_from = max(last_rc or "", billing_start.isoformat()[:7]) or None
            for plan in plan_charges(start=rc_start, end=rc_end, due_day=lease["rent_due_day"],
                                     base_cents=rc["amount_cents"],
                                     prorate_partial=bool(lease["prorate_partial_months"]), method=method,
                                     from_period=rc_from, through=through):
                desc = f"{rc['description'] or LABELS[rc['charge_type']]} {plan.period}"
                if plan.prorated_days:
                    desc += f" (prorated, {plan.prorated_days} days)"
                _insert(conn, result, lock, lease["id"], rc["charge_type"], rc["id"], plan, desc)
    if result.posted:
        audit(conn, "post_rent", changes={"posted": result.posted, "amount": result.amount_cents})
    return result


def _insert(conn, result: PostingResult, lock: date | None, lease_id: int, charge_type: str,
            rc_id: int | None, plan, description: str) -> None:
    if lock and plan.due_date <= lock:
        result.skipped_locked += 1
        return
    cur = conn.execute(
        """INSERT OR IGNORE INTO charges(lease_id, charge_type, period, recurring_charge_id, source,
                                         description, amount_cents, due_date)
           VALUES (?, ?, ?, ?, 'auto', ?, ?, ?)""",
        (lease_id, charge_type, plan.period, rc_id, description, plan.amount_cents, plan.due_date.isoformat()))
    if cur.rowcount:
        result.posted += 1
        result.amount_cents += plan.amount_cents
