"""Lease lifecycle: create, activate, notice, move-out, renewal, rent changes (BLUEPRINT §6.4)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..domain.periods import parse_date, period_of
from ..domain.proration import prorate
from ..domain.rent import rent_in_effect
from . import ledger, rent_posting
from .common import ServiceError, audit, diff, ensure_open, get_setting, row_or_error

STATUSES = ("draft", "future", "active", "month_to_month", "ended", "terminated")
CURRENT = ("active", "month_to_month")
ROLES = ("primary", "co_tenant", "occupant", "guarantor")
LATE_FEE_TYPES = ("none", "flat", "percent")
RECURRING_TYPES = ("pet_rent", "parking", "storage", "utility", "other")
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
    if t.get("rent_due_day") is None or not 1 <= t["rent_due_day"] <= 28:
        raise ServiceError("Rent due day must be between 1 and 28")
    if t.get("late_fee_type") not in LATE_FEE_TYPES:
        raise ServiceError("Choose a late fee type")
    if t["late_fee_type"] == "flat" and not t.get("late_fee_flat_cents"):
        raise ServiceError("Enter the flat late fee amount")
    if t["late_fee_type"] == "percent" and not t.get("late_fee_percent_bp"):
        raise ServiceError("Enter the late fee percentage")
    if t.get("late_fee_grace_days") is None or t["late_fee_grace_days"] < 0:
        raise ServiceError("Grace days cannot be negative")


def create_lease(conn: sqlite3.Connection, *, unit_id: int, tenants: list[tuple[int, str]],
                 start: str, end: str | None, rent_cents: int | None, today: date,
                 draft: bool = False, billing_start: str | None = None,
                 recurring: list[dict] | None = None, **terms: Any) -> int:
    """Create a lease. Status is draft, future (starts later) or active."""
    if rent_cents is None or rent_cents < 0:
        raise ServiceError("Enter the monthly rent")
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
    status = "draft" if draft else ("future" if start_d > today else "active")
    if status == "active":
        existing = _current_lease_on_unit(conn, unit_id)
        if existing:
            raise ServiceError(f"This unit already has a current lease (#{existing['id']}). "
                               "Record the move-out or a renewal first.")
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
    for rc in recurring or []:
        add_recurring_charge(conn, lease_id, **rc)
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


def _set_status(conn, lease_id: int, status: str, **extra: Any) -> None:
    sets = ", ".join(["status = ?", *[f"{k} = ?" for k in extra],
                      "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"])
    conn.execute(f"UPDATE leases SET {sets} WHERE id = ?", (status, *extra.values(), lease_id))
    audit(conn, "status", "lease", lease_id, {"status": status, **extra})


def sign_draft(conn: sqlite3.Connection, lease_id: int, today: date) -> str:
    lease = ledger.lease_row(conn, lease_id)
    if lease["status"] != "draft":
        raise ServiceError("Only draft leases can be marked signed")
    status = "future" if lease["start_date"] > today.isoformat() else "active"
    if status == "active" and _current_lease_on_unit(conn, lease["unit_id"]):
        raise ServiceError("This unit already has a current lease. Record the move-out first.")
    _set_status(conn, lease_id, status)
    if status == "active":
        rent_posting.post_rent(conn, today, [lease_id])
    return status


def delete_draft(conn: sqlite3.Connection, lease_id: int) -> None:
    lease = ledger.lease_row(conn, lease_id)
    if lease["status"] != "draft":
        raise ServiceError("Only draft leases can be deleted")
    conn.execute("DELETE FROM leases WHERE id = ?", (lease_id,))
    audit(conn, "delete", "lease", lease_id)


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


def give_notice(conn: sqlite3.Connection, lease_id: int, notice_date: str, move_out: str) -> None:
    """Record notice to vacate: the lease now ends on the move-out date and the last month is prorated."""
    lease = ledger.lease_row(conn, lease_id)
    if lease["status"] not in CURRENT:
        raise ServiceError("Notice can only be recorded on a current lease")
    move_out_d = parse_date(move_out)
    if move_out_d.isoformat() < lease["start_date"]:
        raise ServiceError("Move-out date is before the lease start")
    _set_status(conn, lease_id, "active", end_date=move_out_d.isoformat(),
                notice_given_date=parse_date(notice_date).isoformat())


@dataclass
class MoveOutResult:
    voided: int
    credit_cents: int


def end_lease(conn: sqlite3.Connection, lease_id: int, move_out: str, status: str = "ended",
              reason: str | None = None) -> MoveOutResult:
    """Record the move-out. Rent billed for days after move-out is voided or credited back."""
    if status not in ("ended", "terminated"):
        raise ServiceError("Unknown end status")
    lease = ledger.lease_row(conn, lease_id)
    if lease["status"] not in (*CURRENT, "future"):
        raise ServiceError("This lease is not current")
    move_out_d = parse_date(move_out)
    start_d = parse_date(lease["start_date"])
    if move_out_d < start_d and lease["status"] != "future":
        raise ServiceError("Move-out date is before the lease start")
    out_period = period_of(move_out_d)
    voided, credit = 0, 0
    changes = [(parse_date(r["effective_date"]), r["rent_cents"]) for r in
               conn.execute("SELECT effective_date, rent_cents FROM lease_rent_changes WHERE lease_id = ?", (lease_id,))]
    recurring = {r["id"]: r for r in conn.execute("SELECT * FROM lease_recurring_charges WHERE lease_id = ?", (lease_id,))}
    for c in conn.execute("SELECT * FROM charges WHERE lease_id = ? AND source = 'auto' AND voided_at IS NULL "
                          "AND period IS NOT NULL AND charge_type <> 'late_fee'", (lease_id,)).fetchall():
        if c["period"] > out_period:
            ledger.void_charge(conn, c["id"], f"Moved out {move_out_d.isoformat()}")
            voided += 1
        elif c["period"] == out_period and lease["prorate_partial_months"]:
            if c["recurring_charge_id"]:
                rc = recurring[c["recurring_charge_id"]]
                base, rc_start = rc["amount_cents"], max(start_d, parse_date(rc["start_date"]))
            else:
                base, rc_start = rent_in_effect(lease["rent_cents"], changes, parse_date(c["due_date"])), start_d
            should = prorate(base, out_period, rc_start, move_out_d, get_setting(conn, "proration_method", "actual_days"))
            if should < c["amount_cents"]:
                credit += c["amount_cents"] - should
    if credit:
        ledger.add_credit(conn, lease_id, credit, move_out_d.isoformat(),
                          f"Proration credit: moved out {move_out_d.isoformat()}")
    end = lease["end_date"]
    if end is None or end > move_out_d.isoformat():
        end = move_out_d.isoformat()
    _set_status(conn, lease_id, status, move_out_date=move_out_d.isoformat(), end_date=end)
    if reason:
        audit(conn, "note", "lease", lease_id, {"end_reason": reason})
    return MoveOutResult(voided, credit)


def renew_lease(conn: sqlite3.Connection, lease_id: int, *, start: str, end: str | None,
                rent_cents: int | None, today: date) -> int:
    """Create the next term as a new lease linked to this one. Tenants, terms and deposit carry over."""
    old = ledger.lease_row(conn, lease_id)
    if old["status"] not in CURRENT:
        raise ServiceError("Only a current lease can be renewed")
    if conn.execute("SELECT 1 FROM leases WHERE renewal_of_lease_id = ? AND status IN ('future','active','draft')",
                    (lease_id,)).fetchone():
        raise ServiceError("This lease already has a renewal")
    start_d = parse_date(start)
    if start_d.isoformat() <= old["start_date"]:
        raise ServiceError("The renewal must start after the current lease started")
    rent_cents = old["rent_cents"] if rent_cents is None else rent_cents
    terms = {k: old[k] for k in TERM_FIELDS if k not in ("notes", "move_in_date")}
    tenants = [(t["id"], t["role"]) for t in lease_tenants(conn, lease_id)]
    day_before = (start_d - timedelta(days=1)).isoformat()
    # The current term now has a definite end, so billing on it stops before the renewal starts.
    _set_status(conn, lease_id, "active", end_date=day_before)
    recurring = [{"charge_type": r["charge_type"], "description": r["description"],
                  "amount_cents": r["amount_cents"], "start": start_d.isoformat(), "end": r["end_date"]}
                 for r in conn.execute("SELECT * FROM lease_recurring_charges WHERE lease_id = ? AND "
                                       "(end_date IS NULL OR end_date >= ?)", (lease_id, start_d.isoformat()))]
    # Insert as future first (the unit still has a current lease), then activate if due.
    new_id = create_lease(conn, unit_id=old["unit_id"], tenants=tenants, start=start_d.isoformat(),
                          end=end, rent_cents=rent_cents, today=date.min, recurring=recurring,
                          move_in_date=old["move_in_date"], **terms)
    conn.execute("UPDATE leases SET renewal_of_lease_id = ? WHERE id = ?", (lease_id, new_id))
    held = ledger.deposit_held(conn, lease_id)
    if held > 0:
        when = max(start_d, today).isoformat()
        ensure_open(conn, when)
        conn.execute("INSERT INTO deposit_transactions(lease_id, txn_date, txn_type, amount_cents, description) "
                     "VALUES (?, ?, 'refund', ?, ?)", (lease_id, when, held, f"Transferred to renewal lease #{new_id}"))
        conn.execute("INSERT INTO deposit_transactions(lease_id, txn_date, txn_type, amount_cents, description) "
                     "VALUES (?, ?, 'received', ?, ?)", (new_id, when, held, f"Transferred from lease #{lease_id}"))
    activate_due(conn, today)
    return new_id


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


def rent_changes(conn: sqlite3.Connection, lease_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM lease_rent_changes WHERE lease_id = ? ORDER BY effective_date",
                        (lease_id,)).fetchall()


def add_recurring_charge(conn: sqlite3.Connection, lease_id: int, *, charge_type: str, description: str,
                         amount_cents: int | None, start: str, end: str | None = None) -> int:
    if charge_type not in RECURRING_TYPES:
        raise ServiceError("Choose a recurring charge type")
    if not amount_cents or amount_cents <= 0:
        raise ServiceError("Amount must be greater than zero")
    if not (description or "").strip():
        raise ServiceError("Describe the recurring charge (e.g. 'Pet rent — 1 dog')")
    cur = conn.execute(
        "INSERT INTO lease_recurring_charges(lease_id, charge_type, description, amount_cents, start_date, end_date) "
        "VALUES (?, ?, ?, ?, ?, ?)", (lease_id, charge_type, description.strip(), amount_cents,
                                      parse_date(start).isoformat(), parse_date(end).isoformat() if end else None))
    audit(conn, "insert", "recurring_charge", cur.lastrowid, {"lease_id": lease_id, "amount": amount_cents})
    return cur.lastrowid


def end_recurring_charge(conn: sqlite3.Connection, rc_id: int, end: str) -> None:
    rc = row_or_error(conn, "SELECT * FROM lease_recurring_charges WHERE id = ?", (rc_id,), "Recurring charge")
    end_iso = parse_date(end).isoformat()
    if end_iso < rc["start_date"]:
        raise ServiceError("End date is before the start date")
    conn.execute("UPDATE lease_recurring_charges SET end_date = ? WHERE id = ?", (end_iso, rc_id))
    audit(conn, "update", "recurring_charge", rc_id, {"end_date": end_iso})


def recurring_charges(conn: sqlite3.Connection, lease_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM lease_recurring_charges WHERE lease_id = ? ORDER BY start_date, id",
                        (lease_id,)).fetchall()


LIST_FILTERS = {
    "current": "l.status IN ('active','month_to_month','future')",
    "active": "l.status = 'active'",
    "month_to_month": "l.status = 'month_to_month'",
    "future": "l.status = 'future'",
    "draft": "l.status = 'draft'",
    "past": "l.status IN ('ended','terminated')",
    "all": "1",
}


def list_leases(conn: sqlite3.Connection, *, status: str = "current", q: str = "",
                expiring_days: int | None = None, today: date | None = None) -> list[sqlite3.Row]:
    where, params = [LIST_FILTERS.get(status, LIST_FILTERS["current"])], []
    if q:
        where.append("(p.code LIKE ? OR p.name LIKE ? OR u.unit_label LIKE ? OR EXISTS ("
                     "SELECT 1 FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id "
                     "WHERE lt.lease_id = l.id AND t.first_name || ' ' || t.last_name LIKE ?))")
        params += [f"%{q}%"] * 4
    if expiring_days is not None and today is not None:
        where.append("l.status = 'active' AND l.end_date IS NOT NULL AND l.end_date BETWEEN ? AND ?")
        params += [today.isoformat(), (today + timedelta(days=expiring_days)).isoformat()]
    return conn.execute(f"""
        SELECT l.*, p.code AS property_code, p.id AS property_id, u.unit_label, cr.current_rent_cents,
               COALESCE(b.balance_cents, 0) AS balance_cents,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN v_lease_current_rent cr ON cr.lease_id = l.id
          LEFT JOIN v_lease_balances b ON b.lease_id = l.id
         WHERE {" AND ".join(where)}
         ORDER BY p.code, u.unit_label, l.start_date DESC""", params).fetchall()


def renewal_of(conn: sqlite3.Connection, lease_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leases WHERE renewal_of_lease_id = ? ORDER BY id DESC LIMIT 1",
                        (lease_id,)).fetchone()
