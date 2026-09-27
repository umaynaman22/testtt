"""Properties and units.

The app shows a flat list of units: each "unit" on screen is a property with
exactly one unit (labelled 'Main') behind the scenes. See flatten_units().
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import search
from .common import ServiceError, audit, diff, row_or_error

PROPERTY_TYPES = ("single_family", "multi_family", "condo", "townhouse", "mobile_home",
                  "commercial", "mixed_use", "other")
PROPERTY_STATUSES = ("active", "sold", "archived")
UNIT_STATUSES = ("active", "offline", "archived")

PROPERTY_FIELDS = ("owner_id", "code", "name", "property_type", "address_line1", "address_line2",
                   "city", "state", "postal_code", "parcel_number", "year_built", "hoa_name",
                   "purchase_date", "purchase_price_cents", "land_value_cents",
                   "placed_in_service_date", "cash_invested_cents", "estimated_value_cents",
                   "value_as_of", "status", "sold_date", "notes")
UNIT_FIELDS = ("unit_label", "bedrooms", "bathrooms", "square_feet", "market_rent_cents", "status", "notes")


def _save(conn, table: str, allowed: tuple[str, ...], row_id: int | None, fields: dict[str, Any],
          entity: str) -> int:
    data = {k: fields.get(k) for k in allowed if k in fields}
    if row_id is None:
        cols = ", ".join(data)
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(data))})",
                           tuple(data.values()))
        audit(conn, "insert", entity, cur.lastrowid, data)
        return cur.lastrowid
    old = row_or_error(conn, f"SELECT * FROM {table} WHERE id = ?", (row_id,), entity.capitalize())
    changes = diff(old, data)
    if changes:
        sets = ", ".join(f"{k} = ?" for k in data)
        extra = ", updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')" if "updated_at" in old.keys() else ""
        conn.execute(f"UPDATE {table} SET {sets}{extra} WHERE id = ?", (*data.values(), row_id))
        audit(conn, "update", entity, row_id, changes)
    return row_id


# ---- properties ---------------------------------------------------------------

def get_property(conn: sqlite3.Connection, pid: int) -> sqlite3.Row:
    return row_or_error(conn, """
        SELECT * FROM properties WHERE id = ?""", (pid,), "Unit")


def default_owner_id(conn: sqlite3.Connection) -> int:
    """Properties need an owner in the database; the simple app uses one behind the scenes."""
    row = conn.execute("SELECT id FROM owners ORDER BY id LIMIT 1").fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO owners(name) VALUES ('Me')").lastrowid


def _unique_code(conn: sqlite3.Connection, base: str, pid: int | None) -> str:
    base = base.strip()[:60] or "Unit"
    code, n = base, 2
    while conn.execute("SELECT 1 FROM properties WHERE code = ? AND id IS NOT ?", (code, pid)).fetchone():
        code = f"{base} ({n})"
        n += 1
    return code


def save_property(conn: sqlite3.Connection, pid: int | None, fields: dict[str, Any],
                  unit_labels: list[str] | None = None, reindex: bool = True) -> int:
    """Create or update a property. Every field is optional.

    The name (usually the street address) doubles as the short label shown
    everywhere. On create, ``unit_labels`` creates its units (None = one unit).
    """
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()}
    existing = get_property(conn, pid) if pid is not None else None
    name = fields.get("name") or fields.get("address_line1") or (existing["name"] if existing else None)
    if not name:
        n = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] + 1
        name = _unique_code(conn, f"Unit {n}", pid)
    fields["name"] = name
    fields["code"] = _unique_code(conn, fields.get("code") or name, pid)
    if "address_line1" in fields or existing is None:
        fields["address_line1"] = fields.get("address_line1") or ""
    for key in ("city", "state", "postal_code"):
        if key in fields or existing is None:
            fields[key] = fields.get(key) or ""
    fields["owner_id"] = fields.get("owner_id") or (existing["owner_id"] if existing else default_owner_id(conn))
    if fields.get("property_type") not in PROPERTY_TYPES:
        fields["property_type"] = (existing["property_type"] if existing else
                                   ("multi_family" if unit_labels and len(unit_labels) > 1 else "single_family"))
    if not fields.get("status"):
        fields["status"] = existing["status"] if existing else "active"
    if fields["status"] not in PROPERTY_STATUSES:
        raise ServiceError("Unknown status")
    new_id = _save(conn, "properties", PROPERTY_FIELDS, pid, fields, "property")
    if pid is None:
        for label in ["Main"] if not unit_labels else unit_labels:
            save_unit(conn, None, new_id, {"unit_label": label}, reindex=False)
    if reindex:
        search.rebuild(conn)
    return new_id


def flatten_units(conn: sqlite3.Connection) -> int:
    """Make every property a single unit, as the screens expect.

    A property with several units becomes one property per unit, named like
    '251 Osmeña St · 2' (a unit called 'Main' keeps the plain name). Leases,
    tenants and payments stay with their unit. A property without units gets
    one. Safe to run any number of times; returns how many units it changed.
    """
    changed = 0
    for p in conn.execute("SELECT * FROM properties ORDER BY id").fetchall():
        units = conn.execute("SELECT id, unit_label FROM units WHERE property_id = ? ORDER BY id",
                             (p["id"],)).fetchall()
        if len(units) == 1 and units[0]["unit_label"] == "Main":
            continue
        if not units:
            save_unit(conn, None, p["id"], {"unit_label": "Main"}, reindex=False)
            changed += 1
            continue
        first, rest = units[0], units[1:]
        for u in rest:  # move these out first, so the first unit can then be renamed 'Main'
            name = p["name"] if u["unit_label"] == "Main" else f"{p['name']} · {u['unit_label']}"
            data = {k: p[k] for k in PROPERTY_FIELDS if k not in ("code", "name", "notes")}
            data.update(name=name, code=_unique_code(conn, name, None))
            new_id = conn.execute(f"INSERT INTO properties ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                                  tuple(data.values())).lastrowid
            conn.execute("UPDATE units SET property_id = ?, unit_label = 'Main' WHERE id = ?", (new_id, u["id"]))
            audit(conn, "split", "property", p["id"], {"unit_id": u["id"], "new_property_id": new_id, "name": name})
            changed += 1
        if first["unit_label"] != "Main":
            name = f"{p['name']} · {first['unit_label']}"
            conn.execute("UPDATE properties SET name = ?, code = ? WHERE id = ?",
                         (name, _unique_code(conn, name, p["id"]), p["id"]))
            conn.execute("UPDATE units SET unit_label = 'Main' WHERE id = ?", (first["id"],))
            audit(conn, "split", "property", p["id"], {"unit_id": first["id"], "name": name})
            changed += 1
    if changed:
        search.rebuild(conn)
    return changed


SORTS = {
    "code": "p.code COLLATE NOCASE",
    "name": "p.name COLLATE NOCASE",
    "city": "p.city COLLATE NOCASE, p.code",
    "balance": "balance_cents DESC, p.code",
}


def list_properties(conn: sqlite3.Connection, *, q: str = "", status: str = "active",
                    sort: str = "code") -> list[sqlite3.Row]:
    """Properties (each one a unit on screen) with the rent and balance of whoever lives there."""
    where, params = [], []
    if status and status != "all":
        where.append("p.status = ?")
        params.append(status)
    if q:
        where.append("(p.code LIKE ? OR p.name LIKE ? OR p.address_line1 LIKE ? OR p.city LIKE ?)")
        params += [f"%{q}%"] * 4
    sql = f"""
        WITH unit_state AS (
            SELECT u.property_id,
                   SUM(COALESCE(b.balance_cents, 0)) AS balance_cents,
                   SUM(COALESCE(cr.current_rent_cents, 0)) AS rent_cents
              FROM units u
              LEFT JOIN leases l ON l.unit_id = u.id AND l.status IN ('active','month_to_month')
              LEFT JOIN v_lease_balances b ON b.lease_id = l.id
              LEFT JOIN v_lease_current_rent cr ON cr.lease_id = l.id
             WHERE u.status = 'active'
             GROUP BY u.property_id)
        SELECT p.*,
               (SELECT MIN(u.id) FROM units u WHERE u.property_id = p.id) AS unit_id,
               COALESCE(us.balance_cents, 0) AS balance_cents,
               COALESCE(us.rent_cents, 0) AS rent_cents
          FROM properties p
          LEFT JOIN unit_state us ON us.property_id = p.id
         {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY {SORTS.get(sort, SORTS["code"])}"""
    return conn.execute(sql, params).fetchall()


# ---- units ------------------------------------------------------------------

def get_unit(conn: sqlite3.Connection, unit_id: int) -> sqlite3.Row:
    return row_or_error(conn, """
        SELECT u.*, p.code AS property_code, p.name AS property_name, p.id AS property_id
          FROM units u JOIN properties p ON p.id = u.property_id WHERE u.id = ?""", (unit_id,), "Unit")


def save_unit(conn: sqlite3.Connection, unit_id: int | None, property_id: int, fields: dict[str, Any],
              reindex: bool = True) -> int:
    fields["unit_label"] = str(fields.get("unit_label") or "").strip()
    if not fields["unit_label"]:
        if unit_id is not None:
            fields["unit_label"] = get_unit(conn, unit_id)["unit_label"]
        else:
            n = conn.execute("SELECT COUNT(*) FROM units WHERE property_id = ?", (property_id,)).fetchone()[0] + 1
            fields["unit_label"] = f"Unit {n}"
    fields.setdefault("status", "active")
    if fields["status"] not in UNIT_STATUSES:
        raise ServiceError("Unknown unit status")
    dup = conn.execute("SELECT id FROM units WHERE property_id = ? AND unit_label = ? AND id IS NOT ?",
                       (property_id, fields["unit_label"], unit_id)).fetchone()
    if dup:
        raise ServiceError(f"This property already has a unit called {fields['unit_label']!r}")
    if unit_id is None:
        data = {"property_id": property_id, **{k: fields.get(k) for k in UNIT_FIELDS if k in fields}}
        cur = conn.execute(f"INSERT INTO units ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                           tuple(data.values()))
        audit(conn, "insert", "unit", cur.lastrowid, data)
        new_id = cur.lastrowid
    else:
        new_id = _save(conn, "units", UNIT_FIELDS, unit_id, fields, "unit")
    if reindex:
        search.rebuild(conn)
    return new_id


def unit_choices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rentable units for the lease form, vacant first."""
    return conn.execute("""
        SELECT u.id, p.code, p.name AS property_name, u.unit_label,
               EXISTS (SELECT 1 FROM leases l WHERE l.unit_id = u.id
                        AND l.status IN ('active','month_to_month')) AS occupied
          FROM units u JOIN properties p ON p.id = u.property_id
         WHERE u.status = 'active' AND p.status = 'active'
         ORDER BY occupied, p.code, u.unit_label""").fetchall()
