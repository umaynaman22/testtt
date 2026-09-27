"""Vendors, expense categories and expenses."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..domain.periods import parse_date
from . import search
from .common import ServiceError, audit, diff, ensure_open, now_utc, row_or_error

VENDOR_FIELDS = ("name", "trade", "contact_name", "phone", "email", "address", "tax_id_last4",
                 "needs_1099", "license_number", "insurance_expires", "is_active", "notes")
PAYMENT_METHODS = ("check", "card", "bank_transfer", "cash", "autopay", "other")


# ---- categories -----------------------------------------------------------------

def list_categories(conn: sqlite3.Connection, active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM expense_categories"
    if active_only:
        sql += " WHERE is_active = 1"
    return conn.execute(sql + " ORDER BY is_capital, name COLLATE NOCASE").fetchall()


def save_category(conn: sqlite3.Connection, cat_id: int | None, *, name: str, tax_line: str | None,
                  is_capital: bool, is_active: bool = True) -> int:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Category name is required")
    dup = conn.execute("SELECT id FROM expense_categories WHERE name = ? COLLATE NOCASE AND id IS NOT ?",
                       (name, cat_id)).fetchone()
    if dup:
        raise ServiceError(f"There is already a category called {name!r}")
    data = {"name": name, "tax_line": (tax_line or "").strip() or None,
            "is_capital": int(bool(is_capital)), "is_active": int(bool(is_active))}
    if cat_id is None:
        cur = conn.execute("INSERT INTO expense_categories(name, tax_line, is_capital, is_active) VALUES (?, ?, ?, ?)",
                           tuple(data.values()))
        audit(conn, "insert", "expense_category", cur.lastrowid, data)
        return cur.lastrowid
    old = row_or_error(conn, "SELECT * FROM expense_categories WHERE id = ?", (cat_id,), "Category")
    conn.execute("UPDATE expense_categories SET name = ?, tax_line = ?, is_capital = ?, is_active = ? WHERE id = ?",
                 (*data.values(), cat_id))
    audit(conn, "update", "expense_category", cat_id, diff(old, data))
    return cat_id


# ---- vendors --------------------------------------------------------------------

def get_vendor(conn: sqlite3.Connection, vendor_id: int) -> sqlite3.Row:
    return row_or_error(conn, "SELECT * FROM vendors WHERE id = ?", (vendor_id,), "Vendor")


def list_vendors(conn: sqlite3.Connection, *, q: str = "", active_only: bool = False,
                 year: int | None = None) -> list[sqlite3.Row]:
    where, params = [], []
    if q:
        where.append("(v.name LIKE ? OR v.trade LIKE ? OR v.phone LIKE ?)")
        params += [f"%{q}%"] * 3
    if active_only:
        where.append("v.is_active = 1")
    ytd = f"{year}-01-01" if year else "0000-01-01"
    return conn.execute(f"""
        SELECT v.*, (SELECT COALESCE(SUM(e.amount_cents), 0) FROM expenses e
                      WHERE e.vendor_id = v.id AND e.voided_at IS NULL AND e.expense_date >= ?) AS paid_cents
          FROM vendors v {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY v.is_active DESC, v.name COLLATE NOCASE""", [ytd, *params]).fetchall()


def save_vendor(conn: sqlite3.Connection, vendor_id: int | None, fields: dict[str, Any], reindex: bool = True) -> int:
    if not (fields.get("name") or "").strip():
        raise ServiceError("Vendor name is required")
    fields["name"] = fields["name"].strip()
    data = {k: fields.get(k) for k in VENDOR_FIELDS if k in fields}
    if vendor_id is None:
        cur = conn.execute(f"INSERT INTO vendors ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                           tuple(data.values()))
        vendor_id = cur.lastrowid
        audit(conn, "insert", "vendor", vendor_id, data)
    else:
        old = get_vendor(conn, vendor_id)
        changes = diff(old, data)
        if changes:
            conn.execute(f"UPDATE vendors SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                         (*data.values(), vendor_id))
            audit(conn, "update", "vendor", vendor_id, changes)
    if reindex:
        search.rebuild(conn)
    return vendor_id


def find_or_create_vendor(conn: sqlite3.Connection, name: str | None) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM vendors WHERE name = ? COLLATE NOCASE ORDER BY is_active DESC LIMIT 1",
                       (name,)).fetchone()
    return row["id"] if row else save_vendor(conn, None, {"name": name})


# ---- expenses -------------------------------------------------------------------

def create_expense(conn: sqlite3.Connection, *, category_id: int | None, expense_date: str,
                   amount_cents: int | None, property_id: int | None = None, unit_id: int | None = None,
                   vendor_id: int | None = None, payment_method: str | None = None,
                   reference: str | None = None, description: str | None = None) -> int:
    if not category_id:
        raise ServiceError("Choose a category")
    if not amount_cents or amount_cents <= 0:
        raise ServiceError("Amount must be greater than zero")
    when = parse_date(expense_date).isoformat()
    ensure_open(conn, when)
    row_or_error(conn, "SELECT id FROM expense_categories WHERE id = ?", (category_id,), "Category")
    if unit_id:
        unit = row_or_error(conn, "SELECT * FROM units WHERE id = ?", (unit_id,), "Unit")
        if property_id and unit["property_id"] != property_id:
            raise ServiceError("That unit belongs to a different property")
        property_id = unit["property_id"]
    if payment_method and payment_method not in PAYMENT_METHODS:
        raise ServiceError("Unknown payment method")
    data = {"property_id": property_id, "unit_id": unit_id, "vendor_id": vendor_id, "category_id": category_id,
            "expense_date": when, "amount_cents": amount_cents, "payment_method": payment_method or None,
            "reference": reference or None, "description": description or None}
    cur = conn.execute(f"INSERT INTO expenses ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                       tuple(data.values()))
    audit(conn, "insert", "expense", cur.lastrowid, data)
    return cur.lastrowid


def void_expense(conn: sqlite3.Connection, expense_id: int, reason: str) -> None:
    e = get_expense(conn, expense_id)
    if e["voided_at"]:
        raise ServiceError("This expense is already voided")
    if not (reason or "").strip():
        raise ServiceError("A reason is required")
    ensure_open(conn, e["expense_date"])
    conn.execute("UPDATE expenses SET voided_at = ?, void_reason = ? WHERE id = ?",
                 (now_utc(), reason.strip(), expense_id))
    audit(conn, "void", "expense", expense_id, {"reason": reason.strip(), "amount": e["amount_cents"]})


def get_expense(conn: sqlite3.Connection, expense_id: int) -> sqlite3.Row:
    return row_or_error(conn, """
        SELECT e.*, c.name AS category_name, c.is_capital, v.name AS vendor_name,
               p.code AS property_code, u.unit_label
          FROM expenses e JOIN expense_categories c ON c.id = e.category_id
          LEFT JOIN vendors v ON v.id = e.vendor_id
          LEFT JOIN properties p ON p.id = e.property_id
          LEFT JOIN units u ON u.id = e.unit_id
         WHERE e.id = ?""", (expense_id,), "Expense")


def list_expenses(conn: sqlite3.Connection, *, start: str | None = None, end: str | None = None,
                  property_ids: list[int] | None = None, category_id: int | None = None,
                  vendor_id: int | None = None, overhead_only: bool = False,
                  include_voided: bool = True, limit: int = 1000) -> list[sqlite3.Row]:
    where, params = [], []
    if start:
        where.append("e.expense_date >= ?")
        params.append(start)
    if end:
        where.append("e.expense_date <= ?")
        params.append(end)
    if property_ids is not None:
        where.append(f"e.property_id IN ({','.join('?' * len(property_ids)) or 'NULL'})")
        params += property_ids
    if overhead_only:
        where.append("e.property_id IS NULL")
    if category_id:
        where.append("e.category_id = ?")
        params.append(category_id)
    if vendor_id:
        where.append("e.vendor_id = ?")
        params.append(vendor_id)
    if not include_voided:
        where.append("e.voided_at IS NULL")
    params.append(limit)
    return conn.execute(f"""
        SELECT e.*, c.name AS category_name, c.is_capital, c.tax_line, v.name AS vendor_name,
               p.code AS property_code, u.unit_label,
               (SELECT COUNT(*) FROM documents d WHERE d.related_type = 'expense' AND d.related_id = e.id) AS doc_count
          FROM expenses e JOIN expense_categories c ON c.id = e.category_id
          LEFT JOIN vendors v ON v.id = e.vendor_id
          LEFT JOIN properties p ON p.id = e.property_id
          LEFT JOIN units u ON u.id = e.unit_id
         {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY e.expense_date DESC, e.id DESC LIMIT ?""", params).fetchall()
