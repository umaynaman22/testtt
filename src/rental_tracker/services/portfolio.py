"""Owners, properties, units and tags."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import search
from .common import ServiceError, audit, diff, row_or_error

ENTITY_TYPES = ("individual", "llc", "corporation", "partnership", "trust", "other")
PROPERTY_TYPES = ("single_family", "multi_family", "condo", "townhouse", "mobile_home",
                  "commercial", "mixed_use", "other")
PROPERTY_STATUSES = ("active", "sold", "archived")
UNIT_STATUSES = ("active", "offline", "archived")

OWNER_FIELDS = ("name", "entity_type", "tax_id_last4", "email", "phone", "mailing_address",
                "management_fee_bp", "notes")
PROPERTY_FIELDS = ("owner_id", "code", "name", "property_type", "address_line1", "address_line2",
                   "city", "state", "postal_code", "parcel_number", "year_built", "hoa_name",
                   "purchase_date", "purchase_price_cents", "land_value_cents",
                   "placed_in_service_date", "cash_invested_cents", "estimated_value_cents",
                   "value_as_of", "status", "sold_date", "notes")
UNIT_FIELDS = ("unit_label", "bedrooms", "bathrooms", "square_feet", "market_rent_cents", "status", "notes")


def _require(fields: dict[str, Any], names: dict[str, str]) -> None:
    missing = [label for key, label in names.items() if fields.get(key) in (None, "")]
    if missing:
        raise ServiceError("Required: " + ", ".join(missing))


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


# ---- owners -----------------------------------------------------------------

def list_owners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT o.*, (SELECT COUNT(*) FROM properties p WHERE p.owner_id = o.id AND p.status = 'active') AS property_count
          FROM owners o ORDER BY o.name COLLATE NOCASE""").fetchall()


def get_owner(conn: sqlite3.Connection, owner_id: int) -> sqlite3.Row:
    return row_or_error(conn, "SELECT * FROM owners WHERE id = ?", (owner_id,), "Owner")


def save_owner(conn: sqlite3.Connection, owner_id: int | None, fields: dict[str, Any],
               reindex: bool = True) -> int:
    _require(fields, {"name": "Name"})
    if fields.get("entity_type") not in ENTITY_TYPES:
        fields["entity_type"] = "individual"
    oid = _save(conn, "owners", OWNER_FIELDS, owner_id, fields, "owner")
    if reindex:
        search.rebuild(conn)
    return oid


# ---- properties ---------------------------------------------------------------

def get_property(conn: sqlite3.Connection, pid: int) -> sqlite3.Row:
    return row_or_error(conn, """
        SELECT p.*, o.name AS owner_name FROM properties p JOIN owners o ON o.id = p.owner_id
         WHERE p.id = ?""", (pid,), "Property")


def default_owner_id(conn: sqlite3.Connection) -> int:
    """Properties need an owner in the database; the simple app uses one behind the scenes."""
    row = conn.execute("SELECT id FROM owners ORDER BY id LIMIT 1").fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO owners(name) VALUES ('Me')").lastrowid


def _unique_code(conn: sqlite3.Connection, base: str, pid: int | None) -> str:
    base = base.strip()[:60] or "Property"
    code, n = base, 2
    while conn.execute("SELECT 1 FROM properties WHERE code = ? AND id IS NOT ?", (code, pid)).fetchone():
        code = f"{base} ({n})"
        n += 1
    return code


def save_property(conn: sqlite3.Connection, pid: int | None, fields: dict[str, Any],
                  tags: list[str] | None = None, unit_labels: list[str] | None = None,
                  reindex: bool = True) -> int:
    """Create or update a property. Every field is optional.

    The name (usually the street address) doubles as the short label shown
    everywhere. On create, ``unit_labels`` creates its units (None = one unit).
    """
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()}
    existing = get_property(conn, pid) if pid is not None else None
    name = fields.get("name") or fields.get("address_line1") or (existing["name"] if existing else None)
    if not name:
        n = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] + 1
        name = _unique_code(conn, f"Property {n}", pid)
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
    if tags is not None:
        set_property_tags(conn, new_id, tags)
    if reindex:
        search.rebuild(conn)
    return new_id


def parse_unit_labels(text: str | None) -> list[str] | None:
    """'3' -> ['1', '2', '3'];  'A, B' -> ['A', 'B'];  '' -> None (one unit)."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit() and 1 <= int(text) <= 200:
        return [str(i) for i in range(1, int(text) + 1)]
    labels = []
    for part in text.split(","):
        part = part.strip()
        if part and part not in labels:
            labels.append(part)
    return labels or None


def set_property_tags(conn: sqlite3.Connection, pid: int, names: list[str]) -> None:
    clean = sorted({n.strip() for n in names if n and n.strip()}, key=str.lower)
    conn.execute("DELETE FROM property_tags WHERE property_id = ?", (pid,))
    for name in clean:
        conn.execute("INSERT INTO tags(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO property_tags(property_id, tag_id) VALUES (?, ?)", (pid, tag_id))
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM property_tags)")


def property_tags(conn: sqlite3.Connection, pid: int) -> list[str]:
    return [r[0] for r in conn.execute("""
        SELECT t.name FROM property_tags pt JOIN tags t ON t.id = pt.tag_id
         WHERE pt.property_id = ? ORDER BY t.name COLLATE NOCASE""", (pid,))]


def all_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT t.id, t.name, COUNT(pt.property_id) AS n FROM tags t
          LEFT JOIN property_tags pt ON pt.tag_id = t.id GROUP BY t.id ORDER BY t.name COLLATE NOCASE""").fetchall()


def property_ids_for(conn: sqlite3.Connection, *, property_id: int | None = None,
                     tag_id: int | None = None, owner_id: int | None = None) -> list[int] | None:
    """Resolve report filters to a list of property ids (None = no filter)."""
    if property_id:
        return [property_id]
    if tag_id:
        return [r[0] for r in conn.execute("SELECT property_id FROM property_tags WHERE tag_id = ?", (tag_id,))]
    if owner_id:
        return [r[0] for r in conn.execute("SELECT id FROM properties WHERE owner_id = ?", (owner_id,))]
    return None


SORTS = {
    "code": "p.code COLLATE NOCASE",
    "name": "p.name COLLATE NOCASE",
    "city": "p.city COLLATE NOCASE, p.code",
    "occupancy": "occupancy_pct, p.code",
    "balance": "balance_cents DESC, p.code",
}


def list_properties(conn: sqlite3.Connection, *, q: str = "", tag_id: int | None = None,
                    owner_id: int | None = None, status: str = "active", city: str = "",
                    vacant_only: bool = False, sort: str = "code") -> list[sqlite3.Row]:
    where, params = [], []
    if status and status != "all":
        where.append("p.status = ?")
        params.append(status)
    if q:
        where.append("(p.code LIKE ? OR p.name LIKE ? OR p.address_line1 LIKE ? OR p.city LIKE ?)")
        params += [f"%{q}%"] * 4
    if tag_id:
        where.append("p.id IN (SELECT property_id FROM property_tags WHERE tag_id = ?)")
        params.append(tag_id)
    if owner_id:
        where.append("p.owner_id = ?")
        params.append(owner_id)
    if city:
        where.append("p.city = ? COLLATE NOCASE")
        params.append(city)
    sql = f"""
        WITH unit_state AS (
            SELECT u.property_id,
                   COUNT(*) AS units,
                   SUM(l.id IS NOT NULL) AS occupied,
                   SUM(COALESCE(b.balance_cents, 0)) AS balance_cents,
                   SUM(COALESCE(cr.current_rent_cents, 0)) AS rent_cents
              FROM units u
              LEFT JOIN leases l ON l.unit_id = u.id AND l.status IN ('active','month_to_month')
              LEFT JOIN v_lease_balances b ON b.lease_id = l.id
              LEFT JOIN v_lease_current_rent cr ON cr.lease_id = l.id
             WHERE u.status = 'active'
             GROUP BY u.property_id)
        SELECT p.*, o.name AS owner_name,
               COALESCE(us.units, 0) AS units, COALESCE(us.occupied, 0) AS occupied,
               CASE WHEN COALESCE(us.units, 0) = 0 THEN NULL
                    ELSE ROUND(100.0 * us.occupied / us.units, 1) END AS occupancy_pct,
               COALESCE(us.balance_cents, 0) AS balance_cents,
               COALESCE(us.rent_cents, 0) AS rent_cents,
               (SELECT group_concat(t.name, ', ') FROM property_tags pt JOIN tags t ON t.id = pt.tag_id
                 WHERE pt.property_id = p.id) AS tags
          FROM properties p
          JOIN owners o ON o.id = p.owner_id
          LEFT JOIN unit_state us ON us.property_id = p.id
         {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY {SORTS.get(sort, SORTS["code"])}"""
    rows = conn.execute(sql, params).fetchall()
    if vacant_only:
        rows = [r for r in rows if r["units"] > r["occupied"]]
    return rows


def cities(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT city FROM properties WHERE status = 'active' ORDER BY city COLLATE NOCASE")]


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


def units_for_property(conn: sqlite3.Connection, pid: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT u.*, l.id AS lease_id, l.status AS lease_status, l.end_date,
               cr.current_rent_cents, COALESCE(b.balance_cents, 0) AS balance_cents,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants,
               (SELECT id FROM leases f WHERE f.unit_id = u.id AND f.status = 'future'
                 ORDER BY f.start_date LIMIT 1) AS future_lease_id
          FROM units u
          LEFT JOIN leases l ON l.unit_id = u.id AND l.status IN ('active','month_to_month')
          LEFT JOIN v_lease_current_rent cr ON cr.lease_id = l.id
          LEFT JOIN v_lease_balances b ON b.lease_id = l.id
         WHERE u.property_id = ?
         ORDER BY u.status = 'archived', u.unit_label""", (pid,)).fetchall()


def unit_choices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rentable units for the lease form, vacant first."""
    return conn.execute("""
        SELECT u.id, p.code, p.name AS property_name, u.unit_label,
               EXISTS (SELECT 1 FROM leases l WHERE l.unit_id = u.id
                        AND l.status IN ('active','month_to_month')) AS occupied
          FROM units u JOIN properties p ON p.id = u.property_id
         WHERE u.status = 'active' AND p.status = 'active'
         ORDER BY occupied, p.code, u.unit_label""").fetchall()


def unit_leases(conn: sqlite3.Connection, unit_id: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT l.*, COALESCE(b.balance_cents, 0) AS balance_cents,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants
          FROM leases l LEFT JOIN v_lease_balances b ON b.lease_id = l.id
         WHERE l.unit_id = ? ORDER BY l.start_date DESC""", (unit_id,)).fetchall()
