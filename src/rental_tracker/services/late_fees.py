"""Finding, approving and waiving late fees (BLUEPRINT §7.4).

A waived fee is stored as a voided auto late-fee charge, which keeps its slot
in the unique index so the same month is never proposed again.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from ..domain.allocation import unpaid_for_period
from ..domain.late_fees import LateFeeTerms, is_assessable, last_grace_day, late_fee_amount
from ..domain.periods import parse_date
from . import ledger
from .common import ServiceError, audit, books_locked_through, get_int_setting, get_setting, now_utc


@dataclass(frozen=True)
class Candidate:
    lease_id: int
    period: str
    rent_due: date
    rent_cents: int
    unpaid_cents: int
    fee_cents: int
    fee_due: date
    property_code: str
    unit_label: str
    tenants: str | None

    @property
    def key(self) -> str:
        return f"{self.lease_id}:{self.period}"


def find_candidates(conn: sqlite3.Connection, today: date) -> list[Candidate]:
    minimum = get_int_setting(conn, "late_fee_min_balance_cents", 0)
    order = ledger.payment_order(conn)
    lock = books_locked_through(conn)
    leases = conn.execute("""
        SELECT l.*, p.code AS property_code, u.unit_label,
               (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                  FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                 WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
         WHERE l.status IN ('active','month_to_month') AND l.late_fee_type <> 'none'
         ORDER BY p.code, u.unit_label""").fetchall()
    if not leases:
        return []
    ledgers = ledger.load_ledgers(conn, [l["id"] for l in leases])
    assessed = {(r[0], r[1]) for r in conn.execute(
        "SELECT lease_id, period FROM charges WHERE source = 'auto' AND charge_type = 'late_fee'")}
    out = []
    for lease in leases:
        charges, payments = ledgers.get(lease["id"], ([], []))
        terms = LateFeeTerms(lease["late_fee_type"], lease["late_fee_grace_days"], lease["late_fee_flat_cents"],
                             lease["late_fee_percent_bp"], lease["late_fee_max_cents"])
        for c in charges:
            if c.charge_type != "rent" or not c.period or (lease["id"], c.period) in assessed:
                continue
            if not is_assessable(c.due_date, terms.grace_days, today):
                continue
            fee_due = last_grace_day(c.due_date, terms.grace_days) + timedelta(days=1)
            if lock and fee_due <= lock:
                continue
            unpaid = unpaid_for_period(charges, payments, c.period,
                                       last_grace_day(c.due_date, terms.grace_days), order)
            if unpaid <= minimum:
                continue
            fee = late_fee_amount(terms, c.amount_cents)
            if fee > 0:
                out.append(Candidate(lease["id"], c.period, c.due_date, c.amount_cents, unpaid, fee, fee_due,
                                     lease["property_code"], lease["unit_label"], lease["tenants"]))
    return out


def _insert(conn: sqlite3.Connection, cand: Candidate, waived: bool) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO charges(lease_id, charge_type, period, source, description, amount_cents,
                                         due_date, voided_at, void_reason)
           VALUES (?, 'late_fee', ?, 'auto', ?, ?, ?, ?, ?)""",
        (cand.lease_id, cand.period, f"Late fee — rent {cand.period}", cand.fee_cents, cand.fee_due.isoformat(),
         now_utc() if waived else None, "Waived" if waived else None))


def apply(conn: sqlite3.Connection, keys: list[str], action: str, today: date) -> int:
    """Approve (post) or waive the selected candidates. Amounts are recomputed, never taken from the form."""
    if action not in ("approve", "waive"):
        raise ServiceError("Unknown action")
    wanted = set(keys)
    n = 0
    for cand in find_candidates(conn, today):
        if cand.key in wanted:
            _insert(conn, cand, waived=action == "waive")
            n += 1
    if n:
        audit(conn, f"late_fees_{action}", changes={"count": n})
    return n


def auto_post(conn: sqlite3.Connection, today: date) -> int:
    if get_setting(conn, "late_fee_mode", "review") != "auto":
        return 0
    cands = find_candidates(conn, today)
    for cand in cands:
        _insert(conn, cand, waived=False)
    if cands:
        audit(conn, "late_fees_auto", changes={"count": len(cands)})
    return len(cands)


def parse_key(key: str) -> tuple[int, str]:
    lease_id, period = key.split(":", 1)
    parse_date(period + "-01")
    return int(lease_id), period
