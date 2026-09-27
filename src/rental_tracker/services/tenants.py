"""Tenant records."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import search
from .common import ServiceError, audit, diff, row_or_error

FIELDS = ("first_name", "last_name", "email", "phone", "alt_phone", "emergency_contact_name",
          "emergency_contact_phone", "forwarding_address", "external_ref", "notes")
_ID_SUFFIX_RE = re.compile(r"#(\d+)\s*$")


def get_tenant(conn: sqlite3.Connection, tid: int) -> sqlite3.Row:
    return row_or_error(conn, "SELECT * FROM tenants WHERE id = ?", (tid,), "Tenant")


def save_tenant(conn: sqlite3.Connection, tid: int | None, fields: dict[str, Any], reindex: bool = True) -> int:
    for key, label in (("first_name", "First name"), ("last_name", "Last name")):
        if not (fields.get(key) or "").strip():
            raise ServiceError(f"{label} is required")
        fields[key] = fields[key].strip()
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


def tenant_label(row: sqlite3.Row) -> str:
    return f"{row['last_name']}, {row['first_name']} #{row['id']}"


def tenant_choices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, first_name, last_name FROM tenants "
                        "ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE").fetchall()


def resolve_tenant(conn: sqlite3.Connection, text: str, phone: str | None = None,
                   email: str | None = None) -> int | None:
    """'Lee, Ann #12' -> existing tenant 12; 'Ann Lee' -> a new tenant. Blank -> None."""
    text = (text or "").strip()
    if not text:
        return None
    m = _ID_SUFFIX_RE.search(text)
    if m:
        return get_tenant(conn, int(m.group(1)))["id"]
    if "," in text:
        last, first = (s.strip() for s in text.split(",", 1))
    else:
        parts = text.rsplit(" ", 1)
        first, last = (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
    if not first or not last:
        raise ServiceError(f"Enter the tenant's first and last name (got {text!r}), or pick an existing tenant")
    return save_tenant(conn, None, {"first_name": first, "last_name": last,
                                    "phone": phone or None, "email": email or None})


def list_tenants(conn: sqlite3.Connection, *, q: str = "", status: str = "current") -> list[sqlite3.Row]:
    where, params = [], []
    if q:
        where.append("(t.first_name || ' ' || t.last_name LIKE ? OR t.last_name || ', ' || t.first_name LIKE ? "
                     "OR t.email LIKE ? OR t.phone LIKE ?)")
        params += [f"%{q}%"] * 4
    current = ("EXISTS (SELECT 1 FROM lease_tenants lt JOIN leases l ON l.id = lt.lease_id "
               "WHERE lt.tenant_id = t.id AND l.status IN ('active','month_to_month','future'))")
    if status == "current":
        where.append(current)
    elif status == "past":
        where.append("NOT " + current)
    return conn.execute(f"""
        SELECT t.*,
               (SELECT p.code || ' · ' || u.unit_label FROM lease_tenants lt
                  JOIN leases l ON l.id = lt.lease_id JOIN units u ON u.id = l.unit_id
                  JOIN properties p ON p.id = u.property_id
                 WHERE lt.tenant_id = t.id ORDER BY l.status IN ('active','month_to_month') DESC,
                       l.start_date DESC LIMIT 1) AS latest_unit,
               (SELECT l.id FROM lease_tenants lt JOIN leases l ON l.id = lt.lease_id
                 WHERE lt.tenant_id = t.id ORDER BY l.status IN ('active','month_to_month') DESC,
                       l.start_date DESC LIMIT 1) AS latest_lease_id
          FROM tenants t
         {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY t.last_name COLLATE NOCASE, t.first_name COLLATE NOCASE""", params).fetchall()


def tenant_leases(conn: sqlite3.Connection, tid: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT l.*, lt.role, p.code AS property_code, p.id AS property_id, u.unit_label,
               COALESCE(b.balance_cents, 0) AS balance_cents
          FROM lease_tenants lt JOIN leases l ON l.id = lt.lease_id
          JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          LEFT JOIN v_lease_balances b ON b.lease_id = l.id
         WHERE lt.tenant_id = ? ORDER BY l.start_date DESC""", (tid,)).fetchall()
