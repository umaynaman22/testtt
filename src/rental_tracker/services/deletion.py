"""Deleting things (BLUEPRINT §7.6). Deletes happen straight away, without asking.

* **Single entries**: a payment, or a line such as a rent bill or a debt.
* **Records with history**: a unit (property), a tenancy (lease) or a tenant.
  ``delete()`` removes it and everything that belongs to it in one transaction;
  the web layer takes a safety backup first.

Every delete writes a copy of the removed row to the audit log, and nothing
dated inside the locked period (books_locked_through) can be deleted.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from ..domain.periods import parse_date
from . import rent_posting, search
from .common import ServiceError, audit, books_locked_through, ensure_open, row_or_error


def _q(ids: list[int]) -> str:
    return ",".join("?" * len(ids)) or "NULL"


def _ids(conn, sql: str, params) -> list[int]:
    return [r[0] for r in conn.execute(sql, tuple(params))]


# ---- single entries ------------------------------------------------------------

def delete_charge(conn: sqlite3.Connection, charge_id: int, today: date) -> str:
    """Delete a charge or credit. A deleted automatic charge is billed again at the current terms."""
    c = row_or_error(conn, "SELECT * FROM charges WHERE id = ?", (charge_id,), "Charge")
    ensure_open(conn, c["due_date"])
    conn.execute("DELETE FROM charges WHERE id = ?", (charge_id,))
    audit(conn, "delete", "charge", charge_id, dict(c))
    what = "Credit" if c["amount_cents"] < 0 else "Charge"
    if c["source"] == "auto" and c["charge_type"] == "late_fee":
        return f"{what} deleted. It will be suggested again under Late fees; waive it there to stop that."
    if c["source"] == "auto":
        lease = conn.execute("SELECT status FROM leases WHERE id = ?", (c["lease_id"],)).fetchone()
        if lease and lease["status"] in ("active", "month_to_month"):
            reposted = rent_posting.post_rent(conn, today, [c["lease_id"]])
            if reposted.posted:
                return (f"{what} deleted and billed again with the lease's current terms. "
                        "To cancel a month for good, void it instead of deleting.")
    return f"{what} deleted."


def delete_payment(conn: sqlite3.Connection, payment_id: int) -> str:
    p = row_or_error(conn, "SELECT * FROM payments WHERE id = ?", (payment_id,), "Payment")
    ensure_open(conn, p["received_date"])
    linked = conn.execute("SELECT * FROM deposit_transactions WHERE payment_id = ?", (payment_id,)).fetchall()
    for t in linked:  # a deposit applied to the balance is one entry seen from two sides
        conn.execute("DELETE FROM deposit_transactions WHERE id = ?", (t["id"],))
        audit(conn, "delete", "deposit_transaction", t["id"], dict(t))
    conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
    audit(conn, "delete", "payment", payment_id, dict(p))
    return "Payment deleted" + (" (and the matching security deposit entry)." if linked else ".")


def _delete_documents(conn, related_type: str, ids: list[int]) -> int:
    """Remove document records. Files stay in the documents folder so restoring a backup still works."""
    if not ids:
        return 0
    return conn.execute(f"DELETE FROM documents WHERE related_type = ? AND related_id IN ({_q(ids)})",
                        (related_type, *ids)).rowcount


# ---- records with history ---------------------------------------------------------

KINDS = ("property", "lease", "tenant")
_TABLES = {"property": "properties", "lease": "leases", "tenant": "tenants"}


@dataclass
class Preview:
    """What a delete is about: a name for messages, where to go afterwards, and anything blocking it."""
    kind: str
    id: int
    label: str
    blockers: list[str] = field(default_factory=list)
    parent: dict = field(default_factory=dict)


def _earliest_money_date(conn, lease_ids: list[int], expense_ids: list[int] = ()) -> str | None:
    dates = []
    if lease_ids:
        q = _q(lease_ids)
        dates += [conn.execute(f"SELECT MIN(due_date) FROM charges WHERE lease_id IN ({q})", lease_ids).fetchone()[0],
                  conn.execute(f"SELECT MIN(received_date) FROM payments WHERE lease_id IN ({q})", lease_ids).fetchone()[0],
                  conn.execute(f"SELECT MIN(txn_date) FROM deposit_transactions WHERE lease_id IN ({q})",
                               lease_ids).fetchone()[0]]
    if expense_ids:
        dates.append(conn.execute(f"SELECT MIN(expense_date) FROM expenses WHERE id IN ({_q(list(expense_ids))})",
                                  list(expense_ids)).fetchone()[0])
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def _lock_blocker(conn, p: Preview, lease_ids: list[int], expense_ids: list[int] = ()) -> None:
    lock = books_locked_through(conn)
    earliest = _earliest_money_date(conn, lease_ids, expense_ids)
    if lock and earliest and parse_date(earliest) <= lock:
        p.blockers.append(f"It has money entries in the locked period (books are locked through {lock.isoformat()}).")


def preview(conn: sqlite3.Connection, kind: str, record_id: int) -> Preview:
    if kind not in KINDS:
        raise ServiceError("That can't be deleted here")
    return globals()[f"_preview_{kind}"](conn, record_id)


def _preview_lease(conn, lease_id: int) -> Preview:
    lease = row_or_error(conn, """
        SELECT l.*, p.code, u.property_id FROM leases l JOIN units u ON u.id = l.unit_id
          JOIN properties p ON p.id = u.property_id WHERE l.id = ?""", (lease_id,), "Lease")
    names = ", ".join(r[0] for r in conn.execute(
        "SELECT t.first_name || ' ' || t.last_name FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id "
        "WHERE lt.lease_id = ?", (lease_id,)))
    p = Preview("lease", lease_id, f"{names or 'this tenant'} at {lease['code']}",
                parent={"property_id": lease["property_id"]})
    _lock_blocker(conn, p, [lease_id])
    return p


def _preview_property(conn, pid: int) -> Preview:
    prop = row_or_error(conn, "SELECT * FROM properties WHERE id = ?", (pid,), "Unit")
    p = Preview("property", pid, prop["name"] or prop["code"])
    leases = _ids(conn, "SELECT l.id FROM leases l JOIN units u ON u.id = l.unit_id WHERE u.property_id = ?", (pid,))
    _lock_blocker(conn, p, leases, _ids(conn, "SELECT id FROM expenses WHERE property_id = ?", (pid,)))
    return p


def _preview_tenant(conn, tid: int) -> Preview:
    t = row_or_error(conn, "SELECT * FROM tenants WHERE id = ?", (tid,), "Tenant")
    p = Preview("tenant", tid, f"{t['first_name']} {t['last_name']}".strip())
    for lease in conn.execute("""
        SELECT p.code, (SELECT COUNT(*) FROM lease_tenants x WHERE x.lease_id = l.id) AS n
          FROM lease_tenants lt JOIN leases l ON l.id = lt.lease_id JOIN units u ON u.id = l.unit_id
          JOIN properties p ON p.id = u.property_id WHERE lt.tenant_id = ?""", (tid,)):
        if lease["n"] == 1:
            p.blockers.append(f"They rent {lease['code']} on their own. Open their page and use Delete there.")
    return p


def validate(p: Preview) -> None:
    """Raise ServiceError unless the delete described by ``p`` may go ahead."""
    if p.blockers:
        raise ServiceError(" ".join(p.blockers))


def delete(conn: sqlite3.Connection, kind: str, record_id: int) -> Preview:
    """Delete a unit, tenancy or tenant with everything that belongs to it."""
    p = preview(conn, kind, record_id)
    validate(p)
    before = row_or_error(conn, f"SELECT * FROM {_TABLES[kind]} WHERE id = ?", (record_id,), kind.capitalize())
    globals()[f"_delete_{kind}"](conn, record_id)
    audit(conn, "delete", kind, record_id, {"record": dict(before)})
    search.rebuild(conn)
    return p


def _purge_leases(conn, lease_ids: list[int]) -> None:
    if not lease_ids:
        return
    q = _q(lease_ids)
    conn.execute(f"DELETE FROM deposit_transactions WHERE lease_id IN ({q})", lease_ids)
    conn.execute(f"DELETE FROM charges WHERE lease_id IN ({q})", lease_ids)
    conn.execute(f"DELETE FROM payments WHERE lease_id IN ({q})", lease_ids)
    conn.execute(f"UPDATE leases SET renewal_of_lease_id = NULL WHERE renewal_of_lease_id IN ({q})", lease_ids)
    conn.execute(f"UPDATE inspections SET lease_id = NULL WHERE lease_id IN ({q})", lease_ids)
    conn.execute(f"UPDATE communications SET lease_id = NULL WHERE lease_id IN ({q})", lease_ids)
    _delete_documents(conn, "lease", lease_ids)
    conn.execute(f"DELETE FROM leases WHERE id IN ({q})", lease_ids)  # tenants, rent changes, add-ons cascade


def _purge_units(conn, unit_ids: list[int]) -> None:
    if not unit_ids:
        return
    q = _q(unit_ids)
    _purge_leases(conn, _ids(conn, f"SELECT id FROM leases WHERE unit_id IN ({q})", unit_ids))
    conn.execute(f"UPDATE expenses SET unit_id = NULL WHERE unit_id IN ({q})", unit_ids)
    conn.execute(f"UPDATE work_orders SET unit_id = NULL WHERE unit_id IN ({q})", unit_ids)
    conn.execute(f"DELETE FROM inspections WHERE unit_id IN ({q})", unit_ids)
    _delete_documents(conn, "unit", unit_ids)
    conn.execute(f"DELETE FROM units WHERE id IN ({q})", unit_ids)


def _purge_property(conn, pid: int) -> None:
    leases = _ids(conn, "SELECT l.id FROM leases l JOIN units u ON u.id = l.unit_id WHERE u.property_id = ?", (pid,))
    people = sorted({t for lid in leases for t in _only_on_lease(conn, lid)})
    expenses = _ids(conn, "SELECT id FROM expenses WHERE property_id = ?", (pid,))
    _delete_documents(conn, "expense", expenses)
    conn.execute("DELETE FROM expenses WHERE property_id = ?", (pid,))
    _purge_units(conn, _ids(conn, "SELECT id FROM units WHERE property_id = ?", (pid,)))
    work_orders = _ids(conn, "SELECT id FROM work_orders WHERE property_id = ?", (pid,))
    if work_orders:
        q = _q(work_orders)
        conn.execute(f"UPDATE charges SET work_order_id = NULL WHERE work_order_id IN ({q})", work_orders)
        conn.execute(f"UPDATE expenses SET work_order_id = NULL WHERE work_order_id IN ({q})", work_orders)
        conn.execute(f"DELETE FROM work_orders WHERE id IN ({q})", work_orders)
    recurring = _ids(conn, "SELECT id FROM recurring_expenses WHERE property_id = ?", (pid,))
    if recurring:
        conn.execute(f"UPDATE expenses SET recurring_expense_id = NULL WHERE recurring_expense_id IN ({_q(recurring)})",
                     recurring)
        conn.execute("DELETE FROM recurring_expenses WHERE property_id = ?", (pid,))
    conn.execute("DELETE FROM loan_payments WHERE loan_id IN (SELECT id FROM loans WHERE property_id = ?)", (pid,))
    conn.execute("DELETE FROM loans WHERE property_id = ?", (pid,))
    conn.execute("DELETE FROM insurance_policies WHERE property_id = ?", (pid,))
    conn.execute("UPDATE mileage_trips SET property_id = NULL WHERE property_id = ?", (pid,))
    _delete_documents(conn, "property", [pid])
    conn.execute("DELETE FROM properties WHERE id = ?", (pid,))  # tags cascade
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM property_tags)")
    for tid in people:
        _delete_tenant(conn, tid)


def _only_on_lease(conn, lease_id: int) -> list[int]:
    """People on this lease who are not on any other lease."""
    return _ids(conn, """SELECT lt.tenant_id FROM lease_tenants lt WHERE lt.lease_id = ? AND NOT EXISTS
                           (SELECT 1 FROM lease_tenants o WHERE o.tenant_id = lt.tenant_id AND o.lease_id <> ?)""",
                (lease_id, lease_id))


def _delete_lease(conn, lease_id: int) -> None:
    people = _only_on_lease(conn, lease_id)
    _purge_leases(conn, [lease_id])
    for tid in people:
        _delete_tenant(conn, tid)


def _delete_property(conn, pid: int) -> None:
    _purge_property(conn, pid)


def _delete_tenant(conn, tid: int) -> None:
    for lease_id in _ids(conn, "SELECT lease_id FROM lease_tenants WHERE tenant_id = ? AND role = 'primary'", (tid,)):
        nxt = conn.execute("SELECT tenant_id FROM lease_tenants WHERE lease_id = ? AND tenant_id <> ? "
                           "ORDER BY role = 'co_tenant' DESC LIMIT 1", (lease_id, tid)).fetchone()
        if nxt:  # someone else becomes the primary tenant
            conn.execute("UPDATE lease_tenants SET role = 'primary' WHERE lease_id = ? AND tenant_id = ?",
                         (lease_id, nxt[0]))
    conn.execute("DELETE FROM lease_tenants WHERE tenant_id = ?", (tid,))
    conn.execute("UPDATE payments SET paid_by_tenant_id = NULL WHERE paid_by_tenant_id = ?", (tid,))
    conn.execute("UPDATE work_orders SET reported_by_tenant_id = NULL WHERE reported_by_tenant_id = ?", (tid,))
    conn.execute("DELETE FROM communications WHERE tenant_id = ?", (tid,))
    _delete_documents(conn, "tenant", [tid])
    conn.execute("DELETE FROM tenants WHERE id = ?", (tid,))
