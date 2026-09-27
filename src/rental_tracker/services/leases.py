"""Tenancies (leases): create, activate, change terms and move-in date, rent changes (BLUEPRINT §6.4)."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from ..domain.periods import parse_date
from . import ledger, rent_posting
from .common import ServiceError, audit, diff, ensure_open, get_setting, row_or_error

CURRENT = ("active", "month_to_month")
ROLES = ("primary", "co_tenant", "occupant", "guarantor")
LATE_FEE_TYPES = ("none", "flat", "percent")
TERM_FIELDS = ("rent_due_day", "prorate_partial_months", "deposit_cents", "late_fee_type",
               "late_fee_grace_days", "late_fee_flat_cents", "late_fee_percent_bp", "late_fee_max_cents",
               "notes", "move_in_date")


def get_lease(conn: sqlite3.Connection, lease_id: int) -> sqlite3.Row:
    return row_or_error(conn, """
        SELECT l.*, u.unit_label, u.property_id, p.code AS property_code, p.name AS property_name,
               p.address_line1, p.address_line2, p.city, p.state, p.postal_code,
               cr.current_rent_cents
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN v_lease_current_rent cr ON cr.lease_id = l.id
         WHERE l.id = ?""", (lease_id,), "Lease")


def lease_tenants(conn: sqlite3.Connection, lease_id: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT t.*, lt.role FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
         WHERE lt.lease_id = ? ORDER BY CASE lt.role WHEN 'primary' THEN 0 WHEN 'co_tenant' THEN 1
                                                     WHEN 'occupant' THEN 2 ELSE 3 END, t.last_name""",
        (lease_id,)).fetchall()


def tenant_names(conn: sqlite3.Connection, lease_id: int) -> str:
    return ", ".join(f"{t['first_name']} {t['last_name']}" for t in lease_tenants(conn, lease_id)
                     if t["role"] in ("primary", "co_tenant"))


def _current_lease_on_unit(conn, unit_id: int, exclude: int | None = None) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leases WHERE unit_id = ? AND status IN ('active','month_to_month') "
                        "AND id IS NOT ?", (unit_id, exclude)).fetchone()


def _validate_terms(t: dict[str, Any]) -> None:
    """Fill sensible defaults for anything left blank, and reject only impossible values."""
    if t.get("rent_due_day") is None:
        t["rent_due_day"] = 1
    if not 1 <= t["rent_due_day"] <= 28:
        raise ServiceError("Rent due day must be between 1 and 28")
    if t.get("late_fee_type") not in LATE_FEE_TYPES:
        t["late_fee_type"] = "none"
    if (t["late_fee_type"] == "flat" and not t.get("late_fee_flat_cents")) or \
            (t["late_fee_type"] == "percent" and not t.get("late_fee_percent_bp")):
        t["late_fee_type"] = "none"  # no amount given means no late fee
    if t.get("late_fee_grace_days") is None:
        t["late_fee_grace_days"] = 5
    if t["late_fee_grace_days"] < 0:
        raise ServiceError("Grace days cannot be negative")


def create_lease(conn: sqlite3.Connection, *, unit_id: int, tenants: list[tuple[int, str]],
                 start: str | None, end: str | None, rent_cents: int | None, today: date,
                 billing_start: str | None = None, **terms: Any) -> int:
    """Create a lease. Status is future (starts later) or active.

    Rent (0 = none) and start date (today) are optional.
    """
    rent_cents = rent_cents or 0
    if rent_cents < 0:
        raise ServiceError("Rent can't be negative")
    start = start or today.isoformat()
    if not tenants:
        raise ServiceError("Add at least one tenant")
    if not any(role == "primary" for _, role in tenants):
        tenants = [(tenants[0][0], "primary")] + list(tenants[1:])
    if len({tid for tid, _ in tenants}) != len(tenants):
        raise ServiceError("The same tenant is listed twice")
    start_d = parse_date(start)
    end_d = parse_date(end) if end else None
    if end_d and end_d < start_d:
        raise ServiceError("The end date is before the start date")
    unit = row_or_error(conn, "SELECT * FROM units WHERE id = ?", (unit_id,), "Unit")
    if unit["status"] != "active":
        raise ServiceError("This unit is not rentable (offline or archived)")
    terms = {"rent_due_day": 1, "prorate_partial_months": 1, "deposit_cents": 0, "late_fee_type": "none",
             "late_fee_grace_days": 5, **{k: v for k, v in terms.items() if v is not None}}
    _validate_terms(terms)
    status = "future" if start_d > today else "active"
    if status == "active":
        existing = _current_lease_on_unit(conn, unit_id)
        if existing:
            raise ServiceError("Someone already lives there. Remove the current tenant first, or use "
                               "'Add another person' on their page for roommates.")
    data = {"unit_id": unit_id, "status": status, "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat() if end_d else None, "rent_cents": rent_cents,
            "billing_start_date": parse_date(billing_start).isoformat() if billing_start else None,
            **{k: terms.get(k) for k in TERM_FIELDS if k in terms}}
    cur = conn.execute(f"INSERT INTO leases ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                       tuple(data.values()))
    lease_id = cur.lastrowid
    for tid, role in tenants:
        if role not in ROLES:
            raise ServiceError("Unknown tenant role")
        conn.execute("INSERT INTO lease_tenants(lease_id, tenant_id, role) VALUES (?, ?, ?)", (lease_id, tid, role))
    audit(conn, "insert", "lease", lease_id, data)
    if status == "active":
        rent_posting.post_rent(conn, today, [lease_id])  # bill what is already due right away
    return lease_id


def update_terms(conn: sqlite3.Connection, lease_id: int, fields: dict[str, Any]) -> None:
    lease = ledger.lease_row(conn, lease_id)
    data = {k: fields[k] for k in TERM_FIELDS if k in fields}
    merged = {**dict(lease), **data}
    _validate_terms(merged)
    if "end_date" in fields and lease["status"] in ("draft", "future", "active"):
        end = parse_date(fields["end_date"]).isoformat() if fields["end_date"] else None
        if end and end < lease["start_date"]:
            raise ServiceError("The end date is before the start date")
        data["end_date"] = end
    if "rent_cents" in fields and fields["rent_cents"] != lease["rent_cents"]:
        posted = conn.execute("SELECT COUNT(*) FROM charges WHERE lease_id = ? AND charge_type = 'rent'",
                              (lease_id,)).fetchone()[0]
        if posted:
            raise ServiceError("Rent has already been billed on this lease. Add a rent change instead.")
        data["rent_cents"] = fields["rent_cents"]
    changes = diff(lease, data)
    if changes:
        conn.execute(f"UPDATE leases SET {', '.join(f'{k} = ?' for k in data)}, "
                     "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (*data.values(), lease_id))
        audit(conn, "update", "lease", lease_id, changes)


def bill_from_move_in(conn: sqlite3.Connection, lease_id: int, move_in: str, today: date) -> None:
    """Set the move-in date and bill rent from it, past months included.

    The automatic rent bills are rebuilt from that date (a partial first month
    is prorated). Payments are kept; they are re-applied oldest first as always.
    """
    lease = ledger.lease_row(conn, lease_id)
    new = parse_date(move_in)
    if lease["status"] not in ("active", "month_to_month", "future"):
        update_terms(conn, lease_id, {"move_in_date": new.isoformat()})
        return
    first = conn.execute("SELECT MIN(due_date) FROM charges WHERE lease_id = ? AND source = 'auto' "
                         "AND charge_type = 'rent'", (lease_id,)).fetchone()[0]
    ensure_open(conn, min(filter(None, [new.isoformat(), first])))
    status = lease["status"]
    if new > today:
        status = "future"
    elif status == "future":
        if _current_lease_on_unit(conn, lease["unit_id"]):
            raise ServiceError("Someone already lives there. Remove the current tenant first.")
        status = "active"
    conn.execute("UPDATE leases SET start_date = ?, move_in_date = ?, billing_start_date = NULL, status = ?, "
                 "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                 (new.isoformat(), new.isoformat(), status, lease_id))
    conn.execute("DELETE FROM charges WHERE lease_id = ? AND source = 'auto' AND charge_type = 'rent'", (lease_id,))
    conn.execute("DELETE FROM charges WHERE lease_id = ? AND source = 'auto' AND charge_type = 'late_fee' "
                 "AND period < ?", (lease_id, new.isoformat()[:7]))  # no late fees before they lived there
    audit(conn, "bill_from_move_in", "lease", lease_id, {"move_in": new.isoformat(), "status": status})
    if status in CURRENT:
        rent_posting.post_rent(conn, today, [lease_id])


def _set_status(conn, lease_id: int, status: str, **extra: Any) -> None:
    sets = ", ".join(["status = ?", *[f"{k} = ?" for k in extra],
                      "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"])
    conn.execute(f"UPDATE leases SET {sets} WHERE id = ?", (status, *extra.values(), lease_id))
    audit(conn, "status", "lease", lease_id, {"status": status, **extra})


def activate_due(conn: sqlite3.Connection, today: date) -> int:
    """Future leases whose start date has arrived become active, ending the lease they replace."""
    n = 0
    for lease in conn.execute("SELECT * FROM leases WHERE status = 'future' AND start_date <= ? ORDER BY start_date",
                              (today.isoformat(),)).fetchall():
        old = _current_lease_on_unit(conn, lease["unit_id"])
        if old:
            day_before = (parse_date(lease["start_date"]) - timedelta(days=1)).isoformat()
            extra: dict[str, Any] = {}
            if old["end_date"] is None or old["end_date"] > day_before:
                extra["end_date"] = day_before
            if lease["renewal_of_lease_id"] != old["id"] and not old["move_out_date"]:
                extra["move_out_date"] = day_before
            _set_status(conn, old["id"], "ended", **extra)
        _set_status(conn, lease["id"], "active")
        rent_posting.post_rent(conn, today, [lease["id"]])
        n += 1
    return n


def roll_expired(conn: sqlite3.Connection, today: date) -> int:
    """Active leases past their end date with no move-out become month-to-month (setting)."""
    if get_setting(conn, "expired_lease_action", "month_to_month") != "month_to_month":
        return 0
    rows = conn.execute("""
        SELECT l.id FROM leases l
         WHERE l.status = 'active' AND l.end_date IS NOT NULL AND l.end_date < ? AND l.move_out_date IS NULL
           AND l.notice_given_date IS NULL
           AND NOT EXISTS (SELECT 1 FROM leases f WHERE f.renewal_of_lease_id = l.id
                            AND f.status IN ('future','active'))""", (today.isoformat(),)).fetchall()
    for r in rows:
        _set_status(conn, r["id"], "month_to_month")
    return len(rows)


def add_rent_change(conn: sqlite3.Connection, lease_id: int, effective: str, rent_cents: int | None,
                    notice_sent: str | None = None, reason: str | None = None) -> int:
    lease = ledger.lease_row(conn, lease_id)
    if rent_cents is None or rent_cents < 0:
        raise ServiceError("Enter the new rent")
    eff = parse_date(effective).isoformat()
    if eff < lease["start_date"]:
        raise ServiceError("The change cannot take effect before the lease starts")
    if conn.execute("SELECT 1 FROM lease_rent_changes WHERE lease_id = ? AND effective_date = ?",
                    (lease_id, eff)).fetchone():
        raise ServiceError("There is already a rent change on that date")
    cur = conn.execute("INSERT INTO lease_rent_changes(lease_id, effective_date, rent_cents, notice_sent_date, reason) "
                       "VALUES (?, ?, ?, ?, ?)", (lease_id, eff, rent_cents,
                                                  parse_date(notice_sent).isoformat() if notice_sent else None,
                                                  reason or None))
    audit(conn, "insert", "rent_change", cur.lastrowid, {"lease_id": lease_id, "effective": eff, "rent": rent_cents})
    return cur.lastrowid
