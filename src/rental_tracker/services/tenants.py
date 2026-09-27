"""Tenant records."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from . import search
from .common import ServiceError, audit, diff, row_or_error

FIELDS = ("first_name", "last_name", "email", "phone", "alt_phone", "emergency_contact_name",
          "emergency_contact_phone", "forwarding_address", "external_ref", "notes")


def get_tenant(conn: sqlite3.Connection, tid: int) -> sqlite3.Row:
    return row_or_error(conn, "SELECT * FROM tenants WHERE id = ?", (tid,), "Tenant")


def save_tenant(conn: sqlite3.Connection, tid: int | None, fields: dict[str, Any], reindex: bool = True) -> int:
    for key in ("first_name", "last_name"):
        if key in fields or tid is None:
            fields[key] = (fields.get(key) or "").strip()
    if tid is None and not fields["first_name"] and not fields["last_name"]:
        fields["first_name"], fields["last_name"] = "Unnamed", "tenant"  # names are optional
    if fields.get("external_ref"):
        dup = conn.execute("SELECT id FROM tenants WHERE external_ref = ? AND id IS NOT ?",
                           (fields["external_ref"], tid)).fetchone()
        if dup:
            raise ServiceError(f"Tenant key {fields['external_ref']!r} is already used by tenant #{dup[0]}")
    else:
        fields["external_ref"] = None
    data = {k: fields.get(k) for k in FIELDS if k in fields}
    if tid is None:
        cur = conn.execute(f"INSERT INTO tenants ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                           tuple(data.values()))
        tid = cur.lastrowid
        audit(conn, "insert", "tenant", tid, data)
    else:
        old = get_tenant(conn, tid)
        changes = diff(old, data)
        if changes:
            conn.execute(f"UPDATE tenants SET {', '.join(f'{k} = ?' for k in data)}, "
                         "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                         (*data.values(), tid))
            audit(conn, "update", "tenant", tid, changes)
    if reindex:
        search.rebuild(conn)
    return tid


def split_name(full: str | None) -> tuple[str, str]:
    """'Ann Lee' -> ('Ann', 'Lee');  'Lee, Ann' -> ('Ann', 'Lee');  'Ann' -> ('Ann', '')."""
    full = " ".join((full or "").split())
    if "," in full:
        last, first = (x.strip() for x in full.split(",", 1))
        return first, last
    if " " in full:
        first, last = full.rsplit(" ", 1)
        return first, last
    return full, ""


def tenant_leases(conn: sqlite3.Connection, tid: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT l.*, lt.role, p.code AS property_code, p.id AS property_id, u.unit_label,
               COALESCE(b.balance_cents, 0) AS balance_cents
          FROM lease_tenants lt JOIN leases l ON l.id = lt.lease_id
          JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          LEFT JOIN v_lease_balances b ON b.lease_id = l.id
         WHERE lt.tenant_id = ? ORDER BY l.start_date DESC""", (tid,)).fetchall()


def tenancies(conn: sqlite3.Connection, *, today: date, status: str = "current", q: str = "",
              property_id: int | None = None) -> list[dict]:
    """One row per tenancy (a lease and the people on it) with balance and how late they are.

    Tenants who were added without a property appear too (status 'current' and 'all').
    """
    from ..domain.allocation import aging, oldest_unpaid_due
    from . import ledger
    where, params = [], []
    if status == "current":
        where.append("l.status IN ('active','month_to_month','future','draft')")
    elif status == "past":
        where.append("l.status IN ('ended','terminated')")
    if property_id:
        where.append("u.property_id = ?")
        params.append(property_id)
    rows = [dict(r) for r in conn.execute(f"""
        SELECT l.id AS lease_id, l.status, l.start_date, l.move_out_date, l.rent_due_day,
               p.id AS property_id, p.code AS property_code, u.id AS unit_id, u.unit_label,
               cr.current_rent_cents, b.balance_cents,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ') FROM lease_tenants lt
                  JOIN tenants t ON t.id = lt.tenant_id WHERE lt.lease_id = l.id) AS names,
               (SELECT t.phone FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND t.phone IS NOT NULL ORDER BY lt.role = 'primary' DESC LIMIT 1) AS phone,
               (SELECT MIN(lt.tenant_id) FROM lease_tenants lt WHERE lt.lease_id = l.id) AS tenant_id,
               (SELECT MAX(pay.received_date) FROM payments pay WHERE pay.lease_id = l.id
                 AND pay.voided_at IS NULL) AS last_paid_on
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN v_lease_balances b ON b.lease_id = l.id JOIN v_lease_current_rent cr ON cr.lease_id = l.id
         {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY p.code COLLATE NOCASE, u.unit_label, l.start_date DESC""", params)]
    owing = [r["lease_id"] for r in rows if r["balance_cents"] > 0]
    ledgers = ledger.load_ledgers(conn, owing) if owing else {}
    order = ledger.payment_order(conn)
    for r in rows:
        r["past_due_cents"], r["days_late"] = 0, 0
        if r["lease_id"] in ledgers:
            charges, payments = ledgers[r["lease_id"]]
            r["past_due_cents"] = aging(charges, payments, today, order)["past_due"]
            oldest = oldest_unpaid_due(charges, payments, order)
            if r["past_due_cents"] and oldest:
                r["days_late"] = (today - oldest).days
        bal = r["balance_cents"]
        r["state"] = "late" if r["past_due_cents"] > 0 else ("owes" if bal > 0 else ("credit" if bal < 0 else "paid"))
    if status in ("current", "all") and not property_id:
        for t in conn.execute("""SELECT * FROM tenants t WHERE NOT EXISTS
                                   (SELECT 1 FROM lease_tenants lt WHERE lt.tenant_id = t.id)
                                 ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE"""):
            rows.append({"lease_id": None, "tenant_id": t["id"], "status": None, "property_id": None,
                         "property_code": None, "unit_id": None, "unit_label": None,
                         "names": f"{t['first_name']} {t['last_name']}".strip(), "phone": t["phone"],
                         "current_rent_cents": None, "balance_cents": 0, "past_due_cents": 0, "days_late": 0,
                         "last_paid_on": None, "state": "none", "start_date": None, "move_out_date": None,
                         "rent_due_day": None})
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in
                                                     ("names", "property_code", "unit_label", "phone")).lower()]
    return rows
