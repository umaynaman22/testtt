"""Tenant ledger: charges, payments and security deposits (BLUEPRINT §7.2, §7.6, §7.7)."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from ..domain.allocation import LedgerCharge, LedgerPayment, aging, allocate, oldest_unpaid_due
from ..domain.periods import parse_date
from .common import ServiceError, audit, ensure_open, get_setting, now_utc, row_or_error, set_setting

CHARGE_TYPES = ("rent", "late_fee", "pet_rent", "parking", "storage", "utility", "damage",
                "repair_billback", "nsf_fee", "legal_fee", "opening_balance", "credit", "other")
MANUAL_CHARGE_TYPES = ("utility", "damage", "repair_billback", "late_fee", "nsf_fee", "legal_fee",
                       "pet_rent", "parking", "storage", "rent", "other")
PAYMENT_METHODS = ("check", "cash", "money_order", "bank_transfer", "card", "app_transfer",
                   "housing_assistance", "other")
DEPOSIT_TYPES = ("received", "interest", "deduction", "refund", "applied_to_balance")
DEPOSIT_OUTFLOWS = ("deduction", "refund", "applied_to_balance")


def _positive(amount: int | None, what: str = "Amount") -> int:
    if amount is None or amount <= 0:
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
    for r in conn.execute("SELECT id, lease_id, amount_cents, received_date FROM payments "
                          f"WHERE voided_at IS NULL{flt}", params):
        out[r["lease_id"]][1].append(LedgerPayment(r["id"], r["amount_cents"],
                                                   date.fromisoformat(r["received_date"])))
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
    return {"balance": lease_balance(conn, lease_id), "aging": ag, "oldest_unpaid_due": oldest,
            "deposit_held": deposit_held(conn, lease_id)}


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
    """Charges and payments in date order with a running balance (voided rows shown, not counted)."""
    rows: list[dict] = []
    for r in conn.execute("SELECT * FROM charges WHERE lease_id = ?", (lease_id,)):
        rows.append({"kind": "charge", "id": r["id"], "date": r["due_date"], "type": r["charge_type"],
                     "description": r["description"] or r["charge_type"].replace("_", " ").capitalize(),
                     "period": r["period"], "charge": r["amount_cents"] if r["amount_cents"] > 0 else 0,
                     "credit": -r["amount_cents"] if r["amount_cents"] < 0 else 0,
                     "voided_at": r["voided_at"], "void_reason": r["void_reason"],
                     "source": r["source"], "sort": (r["due_date"], 0, r["id"])})
    for r in conn.execute("SELECT * FROM payments WHERE lease_id = ?", (lease_id,)):
        desc = f"Payment — {r['method'].replace('_', ' ')}"
        if r["reference"]:
            desc += f" #{r['reference']}"
        rows.append({"kind": "payment", "id": r["id"], "date": r["received_date"], "type": r["method"],
                     "description": desc, "period": None, "charge": 0, "credit": r["amount_cents"],
                     "voided_at": r["voided_at"], "void_reason": r["void_reason"],
                     "receipt_number": r["receipt_number"], "notes": r["notes"],
                     "sort": (r["received_date"], 1, r["id"])})
    rows.sort(key=lambda x: x["sort"])
    running = 0
    for row in rows:
        if not row["voided_at"]:
            running += row["charge"] - row["credit"]
        row["balance"] = running
    return rows


# ---- charges ------------------------------------------------------------------

def add_charge(conn: sqlite3.Connection, lease_id: int, charge_type: str, amount: int | None,
               due: str, description: str | None = None, work_order_id: int | None = None) -> int:
    if charge_type not in CHARGE_TYPES or charge_type in ("credit", "opening_balance"):
        raise ServiceError("Choose a charge type")
    _positive(amount)
    lease_row(conn, lease_id)
    ensure_open(conn, due)
    cur = conn.execute(
        """INSERT INTO charges(lease_id, charge_type, source, description, amount_cents, due_date, work_order_id)
           VALUES (?, ?, 'manual', ?, ?, ?, ?)""",
        (lease_id, charge_type, description or None, amount, parse_date(due).isoformat(), work_order_id))
    audit(conn, "insert", "charge", cur.lastrowid, {"lease_id": lease_id, "type": charge_type, "amount": amount})
    return cur.lastrowid


def add_credit(conn: sqlite3.Connection, lease_id: int, amount: int | None, when: str,
               description: str) -> int:
    _positive(amount)
    if not (description or "").strip():
        raise ServiceError("Say what the credit is for (e.g. 'Concession: paint touch-up')")
    lease_row(conn, lease_id)
    ensure_open(conn, when)
    cur = conn.execute(
        """INSERT INTO charges(lease_id, charge_type, source, description, amount_cents, due_date)
           VALUES (?, 'credit', 'manual', ?, ?, ?)""",
        (lease_id, description.strip(), -amount, parse_date(when).isoformat()))
    audit(conn, "insert", "charge", cur.lastrowid, {"lease_id": lease_id, "type": "credit", "amount": -amount})
    return cur.lastrowid


def void_charge(conn: sqlite3.Connection, charge_id: int, reason: str) -> None:
    c = row_or_error(conn, "SELECT * FROM charges WHERE id = ?", (charge_id,), "Charge")
    if c["voided_at"]:
        raise ServiceError("This charge is already voided")
    if not (reason or "").strip():
        raise ServiceError("A reason is required to void a charge")
    ensure_open(conn, c["due_date"])
    conn.execute("UPDATE charges SET voided_at = ?, void_reason = ? WHERE id = ?",
                 (now_utc(), reason.strip(), charge_id))
    audit(conn, "void", "charge", charge_id, {"reason": reason.strip(), "amount": c["amount_cents"]})


# ---- payments -----------------------------------------------------------------

def next_receipt_number(conn: sqlite3.Connection, year: int) -> str:
    n = int(get_setting(conn, "next_receipt_number", "1") or 1)
    set_setting(conn, "next_receipt_number", str(n + 1))
    return f"{get_setting(conn, 'receipt_number_prefix', 'R-')}{year}-{n:06d}"


def record_payment(conn: sqlite3.Connection, lease_id: int, amount: int | None, received: str,
                   method: str, reference: str | None = None, notes: str | None = None,
                   paid_by_tenant_id: int | None = None, allow_deposit_method: bool = False) -> int:
    _positive(amount)
    if method not in PAYMENT_METHODS and not (allow_deposit_method and method == "deposit_applied"):
        raise ServiceError("Choose a payment method")
    lease_row(conn, lease_id)
    received_d = parse_date(received)
    ensure_open(conn, received_d)
    cur = conn.execute(
        """INSERT INTO payments(lease_id, paid_by_tenant_id, received_date, amount_cents, method,
                                reference, receipt_number, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (lease_id, paid_by_tenant_id, received_d.isoformat(), amount, method, reference or None,
         next_receipt_number(conn, received_d.year), notes or None))
    audit(conn, "insert", "payment", cur.lastrowid, {"lease_id": lease_id, "amount": amount, "method": method})
    return cur.lastrowid


def void_payment(conn: sqlite3.Connection, payment_id: int, reason: str,
                 nsf_fee: int | None = None, today: date | None = None) -> None:
    """Void a payment. For a bounced check, pass an NSF fee to charge the tenant."""
    p = row_or_error(conn, "SELECT * FROM payments WHERE id = ?", (payment_id,), "Payment")
    if p["voided_at"]:
        raise ServiceError("This payment is already voided")
    if not (reason or "").strip():
        raise ServiceError("A reason is required to void a payment")
    if p["method"] == "deposit_applied":
        raise ServiceError("This payment came from the security deposit; void the deposit entry instead")
    ensure_open(conn, p["received_date"])
    conn.execute("UPDATE payments SET voided_at = ?, void_reason = ? WHERE id = ?",
                 (now_utc(), reason.strip(), payment_id))
    audit(conn, "void", "payment", payment_id, {"reason": reason.strip(), "amount": p["amount_cents"]})
    if nsf_fee:
        add_charge(conn, p["lease_id"], "nsf_fee", nsf_fee, (today or date.today()).isoformat(),
                   f"Returned payment fee (receipt {p['receipt_number']})")


def get_payment(conn: sqlite3.Connection, payment_id: int) -> sqlite3.Row:
    return row_or_error(conn, """
        SELECT pay.*, l.unit_id, u.unit_label, p.code AS property_code, p.name AS property_name,
               p.address_line1, p.address_line2, p.city, p.state, p.postal_code, o.name AS owner_name,
               o.phone AS owner_phone, o.mailing_address AS owner_address,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants
          FROM payments pay JOIN leases l ON l.id = pay.lease_id
          JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN owners o ON o.id = p.owner_id
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


# ---- security deposits ---------------------------------------------------------

def deposit_held(conn: sqlite3.Connection, lease_id: int) -> int:
    row = conn.execute("SELECT held_cents FROM v_deposit_held WHERE lease_id = ?", (lease_id,)).fetchone()
    return row[0] if row else 0


def deposit_transactions(conn: sqlite3.Connection, lease_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM deposit_transactions WHERE lease_id = ? ORDER BY txn_date, id",
                        (lease_id,)).fetchall()


def record_deposit(conn: sqlite3.Connection, lease_id: int, txn_type: str, amount: int | None,
                   when: str, description: str | None = None) -> int:
    if txn_type not in DEPOSIT_TYPES:
        raise ServiceError("Choose a deposit transaction type")
    _positive(amount)
    lease_row(conn, lease_id)
    ensure_open(conn, when)
    if txn_type in DEPOSIT_OUTFLOWS and amount > deposit_held(conn, lease_id):
        raise ServiceError("That is more than the deposit currently held")
    if txn_type == "deduction" and not (description or "").strip():
        raise ServiceError("Itemize the deduction (e.g. 'Carpet replacement, bedroom 2')")
    payment_id = None
    if txn_type == "applied_to_balance":
        payment_id = record_payment(conn, lease_id, amount, when, "deposit_applied",
                                    notes="Security deposit applied to balance", allow_deposit_method=True)
    cur = conn.execute(
        """INSERT INTO deposit_transactions(lease_id, txn_date, txn_type, amount_cents, description, payment_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (lease_id, parse_date(when).isoformat(), txn_type, amount, description or None, payment_id))
    audit(conn, "insert", "deposit_transaction", cur.lastrowid,
          {"lease_id": lease_id, "type": txn_type, "amount": amount})
    return cur.lastrowid


def void_deposit(conn: sqlite3.Connection, txn_id: int, reason: str) -> None:
    t = row_or_error(conn, "SELECT * FROM deposit_transactions WHERE id = ?", (txn_id,), "Deposit entry")
    if t["voided_at"]:
        raise ServiceError("This deposit entry is already voided")
    if not (reason or "").strip():
        raise ServiceError("A reason is required")
    ensure_open(conn, t["txn_date"])
    if t["txn_type"] in ("received", "interest") and t["amount_cents"] > deposit_held(conn, t["lease_id"]):
        raise ServiceError("Void the refunds or deductions made from this money first")
    conn.execute("UPDATE deposit_transactions SET voided_at = ?, void_reason = ? WHERE id = ?",
                 (now_utc(), reason.strip(), txn_id))
    if t["payment_id"]:
        conn.execute("UPDATE payments SET voided_at = ?, void_reason = ? WHERE id = ? AND voided_at IS NULL",
                     (now_utc(), reason.strip(), t["payment_id"]))
    audit(conn, "void", "deposit_transaction", txn_id, {"reason": reason.strip()})
