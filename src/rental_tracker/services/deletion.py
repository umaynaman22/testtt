"""Deleting things (BLUEPRINT §7.6).

Two kinds of delete:

* **Single entries** (a payment, charge, credit, expense, deposit entry, rent
  change or add-on). Removed right away after a confirm click. Voiding is still
  available when you want a visible, crossed-out record instead.
* **Records with history** (owner, property, unit, lease, tenant, vendor,
  category). ``preview()`` lists everything that would go and anything that
  blocks it; ``delete()`` then removes it all in one transaction. The web
  layer takes a safety backup first.

Every delete writes a full copy of the removed row to the audit log, and
nothing dated inside the locked period (Settings → books locked through) can be
deleted.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from ..domain.periods import parse_date
from . import rent_posting, search
from .common import ServiceError, audit, books_locked_through, ensure_open, row_or_error

CONFIRM_WORD = "DELETE"


def _q(ids: list[int]) -> str:
    return ",".join("?" * len(ids)) or "NULL"


def _count(conn, sql: str, params) -> int:
    return conn.execute(sql, tuple(params)).fetchone()[0]


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"1 {word}"
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return f"{n} {word[:-1]}ies"
    return f"{n} {word}s"


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


def delete_deposit(conn: sqlite3.Connection, txn_id: int) -> str:
    t = row_or_error(conn, "SELECT * FROM deposit_transactions WHERE id = ?", (txn_id,), "Deposit entry")
    ensure_open(conn, t["txn_date"])
    if t["txn_type"] in ("received", "interest") and not t["voided_at"]:
        held = conn.execute("SELECT COALESCE(held_cents, 0) FROM v_deposit_held WHERE lease_id = ?",
                            (t["lease_id"],)).fetchone()
        if (held[0] if held else 0) < t["amount_cents"]:
            raise ServiceError("Some of this money has already been refunded, kept or applied. "
                               "Delete those entries first.")
    conn.execute("DELETE FROM deposit_transactions WHERE id = ?", (txn_id,))
    audit(conn, "delete", "deposit_transaction", txn_id, dict(t))
    if t["payment_id"]:
        p = conn.execute("SELECT * FROM payments WHERE id = ?", (t["payment_id"],)).fetchone()
        if p:
            conn.execute("DELETE FROM payments WHERE id = ?", (p["id"],))
            audit(conn, "delete", "payment", p["id"], dict(p))
    return "Deposit entry deleted."


def delete_expense(conn: sqlite3.Connection, expense_id: int) -> str:
    e = row_or_error(conn, "SELECT * FROM expenses WHERE id = ?", (expense_id,), "Expense")
    ensure_open(conn, e["expense_date"])
    _delete_documents(conn, "expense", [expense_id])
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    audit(conn, "delete", "expense", expense_id, dict(e))
    return "Expense deleted."


def delete_rent_change(conn: sqlite3.Connection, change_id: int) -> str:
    r = row_or_error(conn, "SELECT * FROM lease_rent_changes WHERE id = ?", (change_id,), "Rent change")
    conn.execute("DELETE FROM lease_rent_changes WHERE id = ?", (change_id,))
    audit(conn, "delete", "rent_change", change_id, dict(r))
    return "Rent change deleted. Rent already billed is not changed."


def delete_recurring_charge(conn: sqlite3.Connection, rc_id: int) -> str:
    """Remove an add-on. Months already billed stay on the ledger as ordinary charges."""
    rc = row_or_error(conn, "SELECT * FROM lease_recurring_charges WHERE id = ?", (rc_id,), "Recurring charge")
    n = conn.execute("UPDATE charges SET recurring_charge_id = NULL, source = 'manual' WHERE recurring_charge_id = ?",
                     (rc_id,)).rowcount
    conn.execute("DELETE FROM lease_recurring_charges WHERE id = ?", (rc_id,))
    audit(conn, "delete", "recurring_charge", rc_id, dict(rc))
    return f"Add-on deleted. {_plural(n, 'month')} already billed stay on the ledger." if n else "Add-on deleted."


def delete_document(conn: sqlite3.Connection, doc_id: int) -> str:
    d = row_or_error(conn, "SELECT * FROM documents WHERE id = ?", (doc_id,), "Document")
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    audit(conn, "delete", "document", doc_id, dict(d))
    return "Document deleted."


def _delete_documents(conn, related_type: str, ids: list[int]) -> int:
    """Remove document records. Files stay in the documents folder so restoring a backup still works."""
    if not ids:
        return 0
    return conn.execute(f"DELETE FROM documents WHERE related_type = ? AND related_id IN ({_q(ids)})",
                        (related_type, *ids)).rowcount


# ---- records with history: preview -------------------------------------------------

@dataclass
class Preview:
    kind: str
    id: int
    label: str                                    # e.g. "property MAPLE-12 (12 Maple St)"
    removes: list[str] = field(default_factory=list)
    keeps: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    money_entries: int = 0                        # payments, charges, deposit entries, expenses removed
    parent: dict = field(default_factory=dict)    # ids used to choose where to go afterwards
    move_choices: list[tuple[int, str]] = field(default_factory=list)  # categories only

    @property
    def needs_typed_confirm(self) -> bool:
        return self.money_entries > 0 or len(self.removes) > 2


KINDS = ("owner", "property", "unit", "lease", "tenant", "vendor", "category")


def _ids(conn, sql: str, params) -> list[int]:
    return [r[0] for r in conn.execute(sql, tuple(params))]


def _lease_money(conn, lease_ids: list[int]) -> dict[str, int]:
    q = _q(lease_ids)
    return {
        "charges": _count(conn, f"SELECT COUNT(*) FROM charges WHERE lease_id IN ({q})", lease_ids),
        "payments": _count(conn, f"SELECT COUNT(*) FROM payments WHERE lease_id IN ({q})", lease_ids),
        "deposits": _count(conn, f"SELECT COUNT(*) FROM deposit_transactions WHERE lease_id IN ({q})", lease_ids),
    }


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
        p.blockers.append(f"It has money entries in the locked period (books are locked through {lock.isoformat()}). "
                          "Change the lock date in Settings first.")


def _describe_leases(conn, p: Preview, lease_ids: list[int]) -> None:
    if not lease_ids:
        return
    money = _lease_money(conn, lease_ids)
    if len(lease_ids) > 1 or p.kind != "lease":
        p.removes.append(_plural(len(lease_ids), "lease"))
    for key, word in (("charges", "charge"), ("payments", "payment"), ("deposits", "security deposit entry")):
        if money[key]:
            p.removes.append(_plural(money[key], word))
    p.money_entries += sum(money.values())
    held = conn.execute(f"SELECT COALESCE(SUM(held_cents), 0) FROM v_deposit_held WHERE lease_id IN ({_q(lease_ids)})",
                        lease_ids).fetchone()[0]
    if held > 0:
        p.warnings.append(f"You are still holding ${held / 100:,.2f} of security deposits for these tenants.")


def preview(conn: sqlite3.Connection, kind: str, record_id: int) -> Preview:
    if kind not in KINDS:
        raise ServiceError("That can't be deleted here")
    return globals()[f"_preview_{kind}"](conn, record_id)


def _preview_lease(conn, lease_id: int) -> Preview:
    lease = row_or_error(conn, """
        SELECT l.*, p.code, u.unit_label, u.property_id FROM leases l JOIN units u ON u.id = l.unit_id
          JOIN properties p ON p.id = u.property_id WHERE l.id = ?""", (lease_id,), "Lease")
    names = ", ".join(r[0] for r in conn.execute(
        "SELECT t.first_name || ' ' || t.last_name FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id "
        "WHERE lt.lease_id = ?", (lease_id,)))
    p = Preview("lease", lease_id, f"the lease for {lease['code']} · {lease['unit_label']}"
                + (f" ({names})" if names else ""), parent={"unit_id": lease["unit_id"]})
    p.removes.append(f"the lease itself ({lease['status'].replace('_', ' ')}, starting {lease['start_date']})")
    _describe_leases(conn, p, [lease_id])
    extras = _count(conn, "SELECT COUNT(*) FROM lease_rent_changes WHERE lease_id = ?", (lease_id,)) + \
        _count(conn, "SELECT COUNT(*) FROM lease_recurring_charges WHERE lease_id = ?", (lease_id,))
    if extras:
        p.removes.append(_plural(extras, "rent change or add-on"))
    p.keeps.append("The tenants' own records (contact details) stay under Tenants.")
    if lease["status"] in ("ended", "terminated"):
        p.warnings.append("This is past history. For taxes and references you may want to keep it.")
    _lock_blocker(conn, p, [lease_id])
    return p


def _preview_unit(conn, unit_id: int) -> Preview:
    u = row_or_error(conn, """SELECT u.*, p.code FROM units u JOIN properties p ON p.id = u.property_id
                               WHERE u.id = ?""", (unit_id,), "Unit")
    p = Preview("unit", unit_id, f"unit {u['unit_label']} of {u['code']}", parent={"property_id": u["property_id"]})
    p.removes.append("the unit")
    leases = _ids(conn, "SELECT id FROM leases WHERE unit_id = ?", (unit_id,))
    _describe_leases(conn, p, leases)
    n_exp = _count(conn, "SELECT COUNT(*) FROM expenses WHERE unit_id = ?", (unit_id,))
    if n_exp:
        p.keeps.append(f"{_plural(n_exp, 'expense')} for this unit stay on the property.")
    if _count(conn, "SELECT COUNT(*) FROM units WHERE property_id = ?", (u["property_id"],)) == 1:
        p.warnings.append("This is the property's only unit. The property will have no units until you add one.")
    p.warnings.append("If the unit is being renovated, you can set its status to Offline instead.")
    _lock_blocker(conn, p, leases)
    return p


def _property_scope(conn, pid: int) -> tuple[list[int], list[int], list[int]]:
    units = _ids(conn, "SELECT id FROM units WHERE property_id = ?", (pid,))
    leases = _ids(conn, f"SELECT id FROM leases WHERE unit_id IN ({_q(units)})", units)
    expenses = _ids(conn, "SELECT id FROM expenses WHERE property_id = ?", (pid,))
    return units, leases, expenses


def _preview_property(conn, pid: int) -> Preview:
    prop = row_or_error(conn, "SELECT * FROM properties WHERE id = ?", (pid,), "Property")
    p = Preview("property", pid, f"property {prop['code']} ({prop['name']})", parent={"owner_id": prop["owner_id"]})
    units, leases, expenses = _property_scope(conn, pid)
    p.removes.append("the property")
    if units:
        p.removes.append(_plural(len(units), "unit"))
    _describe_leases(conn, p, leases)
    if expenses:
        p.removes.append(_plural(len(expenses), "expense"))
        p.money_entries += len(expenses)
    other = sum(_count(conn, f"SELECT COUNT(*) FROM {t} WHERE property_id = ?", (pid,))
                for t in ("loans", "insurance_policies", "work_orders", "recurring_expenses"))
    if other:
        p.removes.append(_plural(other, "loan, policy, work order or recurring bill"))
    if prop["status"] == "active":
        p.warnings.append("Sold it? Set its status to Sold instead (Edit), so its income and expenses stay "
                          "in your reports for taxes.")
    _lock_blocker(conn, p, leases, expenses)
    return p


def _preview_owner(conn, owner_id: int) -> Preview:
    owner = row_or_error(conn, "SELECT * FROM owners WHERE id = ?", (owner_id,), "Owner")
    p = Preview("owner", owner_id, f"owner {owner['name']}")
    p.removes.append("the owner")
    props = _ids(conn, "SELECT id FROM properties WHERE owner_id = ?", (owner_id,))
    all_leases, all_expenses = [], []
    if props:
        p.removes.append(_plural(len(props), "property"))
        n_units = 0
        for pid in props:
            units, leases, expenses = _property_scope(conn, pid)
            n_units += len(units)
            all_leases += leases
            all_expenses += expenses
        if n_units:
            p.removes.append(_plural(n_units, "unit"))
        _describe_leases(conn, p, all_leases)
        if all_expenses:
            p.removes.append(_plural(len(all_expenses), "expense"))
            p.money_entries += len(all_expenses)
        p.warnings.append("To give these properties to another owner instead, change the owner on each property.")
    _lock_blocker(conn, p, all_leases, all_expenses)
    return p


def _preview_tenant(conn, tid: int) -> Preview:
    t = row_or_error(conn, "SELECT * FROM tenants WHERE id = ?", (tid,), "Tenant")
    p = Preview("tenant", tid, f"tenant {t['first_name']} {t['last_name']}")
    p.removes.append("the tenant's record (contact details, notes, documents)")
    for lease in conn.execute("""
        SELECT l.id, p.code, u.unit_label, (SELECT COUNT(*) FROM lease_tenants x WHERE x.lease_id = l.id) AS n
          FROM lease_tenants lt JOIN leases l ON l.id = lt.lease_id JOIN units u ON u.id = l.unit_id
          JOIN properties p ON p.id = u.property_id WHERE lt.tenant_id = ?""", (tid,)):
        if lease["n"] == 1:
            p.blockers.append(f"They are the only tenant on the lease for {lease['code']} · {lease['unit_label']} "
                              f"(lease #{lease['id']}). Delete that lease first, or add another tenant to it.")
        else:
            p.removes.append(f"their place on the lease for {lease['code']} · {lease['unit_label']} "
                             "(the other tenants stay)")
    p.keeps.append("Payments they made stay on the lease ledger.")
    return p


def _preview_vendor(conn, vid: int) -> Preview:
    v = row_or_error(conn, "SELECT * FROM vendors WHERE id = ?", (vid,), "Vendor")
    p = Preview("vendor", vid, f"vendor {v['name']}")
    p.removes.append("the vendor")
    n = _count(conn, "SELECT COUNT(*) FROM expenses WHERE vendor_id = ?", (vid,))
    if n:
        p.keeps.append(f"Their {_plural(n, 'expense')} stay, with no vendor.")
        p.warnings.append("If you just stopped using them, untick Active on the vendor instead.")
    return p


def _preview_category(conn, cid: int) -> Preview:
    c = row_or_error(conn, "SELECT * FROM expense_categories WHERE id = ?", (cid,), "Category")
    p = Preview("category", cid, f"category {c['name']}")
    p.removes.append("the category")
    n = _count(conn, "SELECT COUNT(*) FROM expenses WHERE category_id = ?", (cid,)) + \
        _count(conn, "SELECT COUNT(*) FROM recurring_expenses WHERE category_id = ?", (cid,))
    if n:
        p.keeps.append(f"Its {_plural(n, 'expense')} move to the category you choose below.")
        p.move_choices = [(r["id"], r["name"]) for r in conn.execute(
            "SELECT id, name FROM expense_categories WHERE id <> ? ORDER BY name COLLATE NOCASE", (cid,))]
        if not p.move_choices:
            p.blockers.append("It is the only category. Add another category first.")
    return p


# ---- records with history: delete ---------------------------------------------------

def validate(p: Preview, typed: str | None = None, move_to: int | None = None) -> None:
    """Raise ServiceError unless the delete described by ``p`` may go ahead."""
    if p.blockers:
        raise ServiceError(" ".join(p.blockers))
    if p.needs_typed_confirm and (typed or "").strip().upper() != CONFIRM_WORD:
        raise ServiceError(f"Type {CONFIRM_WORD} in the box to confirm.")
    if p.kind == "category" and p.move_choices and move_to not in {c for c, _ in p.move_choices}:
        raise ServiceError("Choose where its expenses should go.")


def delete(conn: sqlite3.Connection, kind: str, record_id: int, *, typed: str | None = None,
           move_to: int | None = None) -> Preview:
    """Delete after re-checking the preview. Returns the preview that was carried out."""
    p = preview(conn, kind, record_id)
    validate(p, typed, move_to)
    before = row_or_error(conn, f"SELECT * FROM {_TABLES[kind]} WHERE id = ?", (record_id,), kind.capitalize())
    globals()[f"_delete_{kind}"](conn, record_id, move_to)
    audit(conn, "delete", kind, record_id, {"record": dict(before), "also_removed": p.removes})
    if kind in ("owner", "property", "unit", "tenant", "vendor"):
        search.rebuild(conn)
    return p


_TABLES = {"owner": "owners", "property": "properties", "unit": "units", "lease": "leases", "tenant": "tenants",
           "vendor": "vendors", "category": "expense_categories"}


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


def _purge_units(conn, unit_ids: list[int], keep_expenses: bool) -> None:
    if not unit_ids:
        return
    q = _q(unit_ids)
    _purge_leases(conn, _ids(conn, f"SELECT id FROM leases WHERE unit_id IN ({q})", unit_ids))
    if keep_expenses:
        conn.execute(f"UPDATE expenses SET unit_id = NULL WHERE unit_id IN ({q})", unit_ids)
    conn.execute(f"UPDATE work_orders SET unit_id = NULL WHERE unit_id IN ({q})", unit_ids)
    conn.execute(f"DELETE FROM inspections WHERE unit_id IN ({q})", unit_ids)
    _delete_documents(conn, "unit", unit_ids)
    conn.execute(f"DELETE FROM units WHERE id IN ({q})", unit_ids)


def _purge_property(conn, pid: int) -> None:
    expenses = _ids(conn, "SELECT id FROM expenses WHERE property_id = ?", (pid,))
    _delete_documents(conn, "expense", expenses)
    conn.execute("DELETE FROM expenses WHERE property_id = ?", (pid,))
    _purge_units(conn, _ids(conn, "SELECT id FROM units WHERE property_id = ?", (pid,)), keep_expenses=False)
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


def _delete_lease(conn, lease_id: int, _move_to=None) -> None:
    _purge_leases(conn, [lease_id])


def _delete_unit(conn, unit_id: int, _move_to=None) -> None:
    _purge_units(conn, [unit_id], keep_expenses=True)


def _delete_property(conn, pid: int, _move_to=None) -> None:
    _purge_property(conn, pid)


def _delete_owner(conn, owner_id: int, _move_to=None) -> None:
    for pid in _ids(conn, "SELECT id FROM properties WHERE owner_id = ?", (owner_id,)):
        _purge_property(conn, pid)
    conn.execute("UPDATE bank_accounts SET owner_id = NULL WHERE owner_id = ?", (owner_id,))
    _delete_documents(conn, "owner", [owner_id])
    conn.execute("DELETE FROM owners WHERE id = ?", (owner_id,))


def _delete_tenant(conn, tid: int, _move_to=None) -> None:
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


def _delete_vendor(conn, vid: int, _move_to=None) -> None:
    for table in ("expenses", "work_orders", "recurring_expenses"):
        conn.execute(f"UPDATE {table} SET vendor_id = NULL WHERE vendor_id = ?", (vid,))
    _delete_documents(conn, "vendor", [vid])
    conn.execute("DELETE FROM vendors WHERE id = ?", (vid,))


def _delete_category(conn, cid: int, move_to: int | None) -> None:
    if move_to:
        conn.execute("UPDATE expenses SET category_id = ? WHERE category_id = ?", (move_to, cid))
        conn.execute("UPDATE recurring_expenses SET category_id = ? WHERE category_id = ?", (move_to, cid))
    conn.execute("DELETE FROM expense_categories WHERE id = ?", (cid,))
