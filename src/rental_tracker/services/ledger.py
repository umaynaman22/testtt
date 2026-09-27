"""Tenant ledger: charges (rent, debts, late fees) and payments (BLUEPRINT §7.2, §7.6)."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from ..domain.allocation import LedgerCharge, LedgerPayment, aging, allocate, oldest_unpaid_due
from ..domain.money import format_money
from ..domain.periods import parse_date
from .common import ServiceError, audit, ensure_open, get_setting, row_or_error, set_setting

CHARGE_TYPES = ("rent", "late_fee", "pet_rent", "parking", "storage", "utility", "damage",
                "repair_billback", "nsf_fee", "legal_fee", "opening_balance", "credit", "other")
PAYMENT_METHODS = ("cash", "check", "bank_transfer", "gcash", "other")
METHOD_LABELS = {"cash": "Cash", "check": "Check", "bank_transfer": "Bank transfer", "gcash": "GCash",
                 "other": "Other", "app_transfer": "App transfer", "deposit_applied": "From deposit"}


def method_name(method: str | None, other: str | None = None) -> str:
    """How a payment was made, for screens and receipts: 'GCash', or what was typed for Other."""
    if method == "other" and other:
        return other
    return METHOD_LABELS.get(method or "", (method or "").replace("_", " ").capitalize())


def _positive(amount: int | None, what: str = "Amount") -> int:
    if amount is None:
        raise ServiceError(f"Enter {what[0].lower() + what[1:]}")
    if amount <= 0:
        raise ServiceError(f"{what} must be greater than zero")
    return amount


def lease_row(conn: sqlite3.Connection, lease_id: int) -> sqlite3.Row:
    return row_or_error(conn, "SELECT * FROM leases WHERE id = ?", (lease_id,), "Lease")


# ---- loading ------------------------------------------------------------------

def load_ledgers(conn: sqlite3.Connection, lease_ids: Iterable[int] | None = None
                 ) -> dict[int, tuple[list[LedgerCharge], list[LedgerPayment]]]:
    """Non-voided charges and payments per lease, in two queries."""
    ids = None if lease_ids is None else list(lease_ids)
    out: dict[int, tuple[list, list]] = defaultdict(lambda: ([], []))
    if ids is not None and not ids:
        return {}
    flt, params = "", ()
    if ids is not None:
        flt = f" AND lease_id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)
    for r in conn.execute("SELECT id, lease_id, charge_type, amount_cents, due_date, period FROM charges "
                          f"WHERE voided_at IS NULL{flt}", params):
        out[r["lease_id"]][0].append(LedgerCharge(r["id"], r["charge_type"], r["amount_cents"],
                                                  date.fromisoformat(r["due_date"]), r["period"]))
    for r in conn.execute("SELECT id, lease_id, amount_cents, received_date, charge_id FROM payments "
                          f"WHERE voided_at IS NULL{flt}", params):
        out[r["lease_id"]][1].append(LedgerPayment(r["id"], r["amount_cents"],
                                                   date.fromisoformat(r["received_date"]), r["charge_id"]))
    return dict(out)


def payment_order(conn: sqlite3.Connection) -> str:
    return get_setting(conn, "payment_application_order", "oldest_first_rent_before_fees")


def lease_balance(conn: sqlite3.Connection, lease_id: int) -> int:
    return conn.execute("SELECT balance_cents FROM v_lease_balances WHERE lease_id = ?",
                        (lease_id,)).fetchone()[0]


def lease_summary(conn: sqlite3.Connection, lease_id: int, today: date) -> dict:
    charges, payments = load_ledgers(conn, [lease_id]).get(lease_id, ([], []))
    order = payment_order(conn)
    ag = aging(charges, payments, today, order)
    oldest = oldest_unpaid_due(charges, payments, order)
    return {"balance": lease_balance(conn, lease_id), "aging": ag, "oldest_unpaid_due": oldest}


def period_status(conn: sqlite3.Connection, period: str, lease_ids: Iterable[int] | None = None
                  ) -> dict[int, tuple[int, int]]:
    """{lease_id: (billed, still unpaid)} for a month's rent and recurring charges.

    'Unpaid' uses the payment application order, so rent paid early (e.g. on the
    28th of the month before) correctly counts as paid.
    """
    order = payment_order(conn)
    out = {}
    for lid, (charges, payments) in load_ledgers(conn, lease_ids).items():
        mine = [c for c in charges if c.period == period and c.charge_type != "late_fee" and c.amount_cents > 0]
        if not mine:
            continue
        alloc = allocate(charges, payments, order)
        out[lid] = (sum(c.amount_cents for c in mine), sum(alloc.unpaid.get(c.id, 0) for c in mine))
    return out


def ledger_entries(conn: sqlite3.Connection, lease_id: int) -> list[dict]:
    """Charges and payments in date order with a running balance. Deleted (voided) lines are left out."""
    rows: list[dict] = []
    charges, payments = load_ledgers(conn, [lease_id]).get(lease_id, ([], []))
    unpaid = allocate(charges, payments, payment_order(conn)).unpaid
    names = {}
    for r in conn.execute("SELECT * FROM charges WHERE lease_id = ?", (lease_id,)):
        names[r["id"]] = r["description"] or r["charge_type"].replace("_", " ").capitalize()
        if r["voided_at"]:
            continue
        rows.append({"kind": "charge", "id": r["id"], "date": r["due_date"], "type": r["charge_type"],
                     "description": names[r["id"]],
                     "period": r["period"], "charge": r["amount_cents"] if r["amount_cents"] > 0 else 0,
                     "credit": -r["amount_cents"] if r["amount_cents"] < 0 else 0,
                     "source": r["source"], "sort": (r["due_date"], 0, r["id"]),
                     "unpaid": unpaid.get(r["id"]) if r["amount_cents"] > 0 else None})
    for r in conn.execute("SELECT * FROM payments WHERE lease_id = ? AND voided_at IS NULL", (lease_id,)):
        desc = f"Payment — {method_name(r['method'], r['method_other'])}"
        if r["charge_id"] in names:
            desc += f", for {names[r['charge_id']]}"
        rows.append({"kind": "payment", "id": r["id"], "date": r["received_date"], "type": r["method"],
                     "description": desc, "period": None, "charge": 0, "credit": r["amount_cents"],
                     "receipt_number": r["receipt_number"], "notes": r["notes"], "unpaid": None,
                     "method_other": r["method_other"],
                     "sort": (r["received_date"], 1, r["id"])})
    rows.sort(key=lambda x: x["sort"])
    running = 0
    for row in rows:
        running += row["charge"] - row["credit"]
        row["balance"] = running
    return rows


# ---- charges ------------------------------------------------------------------

def add_charge(conn: sqlite3.Connection, lease_id: int, charge_type: str | None, amount: int | None,
               due: str | None, description: str | None = None) -> int:
    charge_type = charge_type or "other"
    due = due or date.today().isoformat()
    if charge_type not in CHARGE_TYPES or charge_type == "credit":
        raise ServiceError("Unknown charge type")
    _positive(amount)
    lease_row(conn, lease_id)
    ensure_open(conn, due)
    cur = conn.execute(
        """INSERT INTO charges(lease_id, charge_type, source, description, amount_cents, due_date)
           VALUES (?, ?, 'manual', ?, ?, ?)""",
        (lease_id, charge_type, description or None, amount, parse_date(due).isoformat()))
    audit(conn, "insert", "charge", cur.lastrowid, {"lease_id": lease_id, "type": charge_type, "amount": amount})
    return cur.lastrowid


# ---- payments -----------------------------------------------------------------

def next_receipt_number(conn: sqlite3.Connection, year: int) -> str:
    n = int(get_setting(conn, "next_receipt_number", "1") or 1)
    set_setting(conn, "next_receipt_number", str(n + 1))
    return f"{get_setting(conn, 'receipt_number_prefix', 'R-')}{year}-{n:06d}"


def record_payment(conn: sqlite3.Connection, lease_id: int, amount: int | None, received: str | None,
                   method: str | None, notes: str | None = None, method_other: str | None = None,
                   charge_id: int | None = None) -> int:
    """Record money received. Date defaults to today and method to 'other'.

    method_other is what was typed for "Other" (e.g. 'PayMaya'); it's ignored for the other methods.
    charge_id makes it a payment toward that line of the history (see domain.allocation).
    """
    _positive(amount, "The amount paid")
    received = received or date.today().isoformat()
    method = method or "other"
    if method not in PAYMENT_METHODS:
        raise ServiceError("Unknown payment method")
    method_other = (" ".join((method_other or "").split())[:60] or None) if method == "other" else None
    lease_row(conn, lease_id)
    if charge_id is not None and not conn.execute("SELECT 1 FROM charges WHERE id = ? AND lease_id = ?",
                                                  (charge_id, lease_id)).fetchone():
        raise ServiceError("That line isn't on this tenant's account")
    received_d = parse_date(received)
    ensure_open(conn, received_d)
    cur = conn.execute(
        """INSERT INTO payments(lease_id, received_date, amount_cents, method, method_other,
                                receipt_number, notes, charge_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (lease_id, received_d.isoformat(), amount, method, method_other,
         next_receipt_number(conn, received_d.year), notes or None, charge_id))
    audit(conn, "insert", "payment", cur.lastrowid, {"lease_id": lease_id, "amount": amount, "method": method,
                                                         "method_other": method_other})
    return cur.lastrowid


def unpaid_rent(conn: sqlite3.Connection, lease_id: int, through: date) -> list[tuple[LedgerCharge, int]]:
    """Rent bills due on or before ``through`` that aren't fully paid, oldest first, with what's unpaid."""
    charges, payments = load_ledgers(conn, [lease_id]).get(lease_id, ([], []))
    alloc = allocate(charges, payments, payment_order(conn))
    return [(c, alloc.unpaid[c.id]) for c in sorted(charges, key=lambda c: (c.due_date, c.id))
            if c.charge_type == "rent" and c.due_date <= through and alloc.unpaid.get(c.id, 0) > 0]


def fill_rent_paid(conn: sqlite3.Connection, lease_id: int, through: date, today: date,
                   method: str | None = None, method_other: str | None = None) -> tuple[int, int]:
    """Mark rent as paid up to a date: one payment per unpaid rent bill, dated on its due date.

    For months a tenant paid before you started using the app. Returns (payments, total cents).
    """
    through = min(through, today)
    items = unpaid_rent(conn, lease_id, through)
    for c, amount in items:
        record_payment(conn, lease_id, amount, c.due_date.isoformat(), method,
                       notes=f"Filled in: rent {c.period or c.due_date.isoformat()}", method_other=method_other,
                       charge_id=c.id)
    return len(items), sum(amount for _, amount in items)


def edit_line(conn: sqlite3.Connection, lease_id: int, kind: str, entry_id: int, *, date: str | None = None,
              description: str | None = None, amount: int | None = None, method: str | None = None,
              method_other: str | None = None) -> dict:
    """Change one line of a tenant's payment history; only the values given change.

    A bill (kind 'charge'): due date, description, amount. A payment: date received, amount,
    how they paid. Balances are recalculated from the lines, as always. Returns what changed.
    """
    table, date_col = {"payment": ("payments", "received_date"), "charge": ("charges", "due_date")}[kind]
    row = row_or_error(conn, f"SELECT * FROM {table} WHERE id = ? AND lease_id = ?", (entry_id, lease_id), "Line")
    new: dict = {}
    if date:
        new[date_col] = parse_date(date).isoformat()
    if amount is not None:
        _positive(amount, "The amount")
        new["amount_cents"] = -amount if kind == "charge" and row["amount_cents"] < 0 else amount
    if kind == "charge" and description is not None:
        new["description"] = " ".join(description.split())[:120] or None
    if kind == "payment" and method is not None:
        if row["method"] == "deposit_applied":
            raise ServiceError("This payment came from the security deposit")
        if method not in PAYMENT_METHODS:
            raise ServiceError("Unknown payment method")
        new["method"] = method
        new["method_other"] = (" ".join((method_other or "").split())[:60] or None) if method == "other" else None
    changes = {k: [row[k], v] for k, v in new.items() if row[k] != v}
    if changes:
        ensure_open(conn, min(row[date_col], new.get(date_col, row[date_col])))
        conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                     (*[v for _, v in changes.values()], entry_id))
        audit(conn, "update", kind, entry_id, changes)
    return changes


def pay_line(conn: sqlite3.Connection, lease_id: int, charge_id: int, amount: int | None, today: date) -> dict:
    """Mark one line of the history (a rent bill, a debt…) as paid, or partly paid.

    Records a payment toward that line, dated on its due date (never in the future), with
    the tenant's usual payment method. Blank amount = whatever is left on the line.
    """
    c = row_or_error(conn, "SELECT * FROM charges WHERE id = ? AND lease_id = ?", (charge_id, lease_id), "Line")
    if c["voided_at"] or c["amount_cents"] <= 0:
        raise ServiceError("This line isn't something to pay")
    charges, payments = load_ledgers(conn, [lease_id]).get(lease_id, ([], []))
    left = allocate(charges, payments, payment_order(conn)).unpaid.get(charge_id, 0)
    if left <= 0:
        raise ServiceError("This line is already paid")
    amount = left if amount is None else amount
    if amount > left:
        raise ServiceError(f"That's more than the {format_money(left)} left on this line")
    last = conn.execute("SELECT method, method_other FROM payments WHERE lease_id = ? AND voided_at IS NULL "
                        "ORDER BY received_date DESC, id DESC LIMIT 1", (lease_id,)).fetchone()
    method = last["method"] if last and last["method"] in PAYMENT_METHODS else None
    when = min(parse_date(c["due_date"]), today)
    pid = record_payment(conn, lease_id, amount, when.isoformat(), method, charge_id=charge_id,
                         method_other=last["method_other"] if method == "other" else None)
    return {"payment_id": pid, "amount": amount, "left": left - amount,
            "line": c["description"] or c["charge_type"].replace("_", " ").capitalize()}


def get_payment(conn: sqlite3.Connection, payment_id: int) -> sqlite3.Row:
    return row_or_error(conn, """
        SELECT pay.*, l.unit_id, u.unit_label, p.code AS property_code, p.name AS property_name, p.city,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants
          FROM payments pay JOIN leases l ON l.id = pay.lease_id
          JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
         WHERE pay.id = ?""", (payment_id,), "Payment")


def list_payments(conn: sqlite3.Connection, *, start: str | None = None, end: str | None = None,
                  method: str | None = None, property_ids: list[int] | None = None,
                  include_voided: bool = True, limit: int = 500) -> list[sqlite3.Row]:
    where, params = [], []
    if start:
        where.append("pay.received_date >= ?")
        params.append(start)
    if end:
        where.append("pay.received_date <= ?")
        params.append(end)
    if method:
        where.append("pay.method = ?")
        params.append(method)
    if property_ids is not None:
        where.append(f"p.id IN ({','.join('?' * len(property_ids)) or 'NULL'})")
        params += property_ids
    if not include_voided:
        where.append("pay.voided_at IS NULL")
    params.append(limit)
    return conn.execute(f"""
        SELECT pay.*, p.code AS property_code, p.id AS property_id, u.unit_label,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = pay.lease_id AND lt.role IN ('primary','co_tenant')) AS tenants
          FROM payments pay JOIN leases l ON l.id = pay.lease_id
          JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
         {"WHERE " + " AND ".join(where) if where else ""}
         ORDER BY pay.received_date DESC, pay.id DESC LIMIT ?""", params).fetchall()
