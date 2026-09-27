"""Reports (BLUEPRINT §10). Each returns a Report that the UI renders and exports to CSV."""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..domain.allocation import BUCKETS, aging
from ..domain.periods import add_periods, parse_date, period_end, period_of, period_start
from . import ledger
from .common import csv_row, get_int_setting


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = "text"  # text | money | date | pct | int
    link: tuple[str, str] | None = None  # (entity type, id key in row)


@dataclass
class Report:
    key: str
    title: str
    subtitle: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    totals: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c.label for c in self.columns])
        for row in self.rows + ([self.totals] if self.totals else []):
            w.writerow(csv_row([_csv_value(row.get(c.key), c.kind) for c in self.columns]))
        return buf.getvalue()


def _csv_value(v: Any, kind: str) -> Any:
    if v is None:
        return ""
    if kind == "money":
        sign = "-" if v < 0 else ""
        return f"{sign}{abs(v) // 100}.{abs(v) % 100:02d}"
    return v


def _in(col: str, ids: list[int] | None) -> tuple[str, list[int]]:
    if ids is None:
        return "1", []
    if not ids:
        return "0", []
    return f"{col} IN ({','.join('?' * len(ids))})", list(ids)


def _sum_totals(rows: list[dict], columns: list[Column], label_key: str, label: str = "Total") -> dict:
    totals: dict[str, Any] = {label_key: label, "_class": "total"}
    for c in columns:
        if c.kind == "money" and c.key != label_key:
            totals[c.key] = sum(r.get(c.key) or 0 for r in rows)
    return totals


TENANTS_SQL = """(SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
                    FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
                   WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant'))"""


# ---- 1. Rent roll ----------------------------------------------------------------

def rent_roll(conn: sqlite3.Connection, property_ids: list[int] | None = None) -> Report:
    flt, params = _in("property_id", property_ids)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM v_rent_roll WHERE {flt} ORDER BY property_code, unit_label", params)]
    cols = [Column("property_code", "Property", link=("property", "property_id")),
            Column("unit_label", "Unit", link=("unit", "unit_id")),
            Column("tenants", "Tenants", link=("lease", "lease_id")),
            Column("lease_status", "Status"), Column("start_date", "Start", "date"),
            Column("end_date", "End", "date"), Column("current_rent_cents", "Rent", "money"),
            Column("market_rent_cents", "Market rent", "money"),
            Column("deposit_held_cents", "Deposit held", "money"),
            Column("balance_cents", "Balance", "money")]
    for r in rows:
        if r["occupancy"] == "vacant":
            r["tenants"], r["lease_status"] = "— vacant —", ""
    occupied = sum(r["occupancy"] == "occupied" for r in rows)
    sub = f"{len(rows)} units · {occupied} occupied"
    if rows:
        sub += f" · {100 * occupied / len(rows):.1f}% occupancy"
    return Report("rent-roll", "Rent roll", sub, cols, rows, _sum_totals(rows, cols, "property_code"))


# ---- 2. Delinquency / aging --------------------------------------------------------

def aging_report(conn: sqlite3.Connection, today: date, property_ids: list[int] | None = None,
                 include_credits: bool = False) -> Report:
    flt, params = _in("u.property_id", property_ids)
    leases = conn.execute(f"""
        SELECT l.id AS lease_id, l.status, p.code AS property_code, p.id AS property_id, u.unit_label,
               {TENANTS_SQL} AS tenants, b.balance_cents
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN v_lease_balances b ON b.lease_id = l.id
         WHERE b.balance_cents <> 0 AND {flt}
         ORDER BY p.code, u.unit_label""", params).fetchall()
    ledgers = ledger.load_ledgers(conn, [r["lease_id"] for r in leases])
    order = ledger.payment_order(conn)
    rows = []
    for r in leases:
        charges, payments = ledgers.get(r["lease_id"], ([], []))
        ag = aging(charges, payments, today, order)
        if not include_credits and ag["past_due"] == 0 and ag["current"] == 0:
            continue
        rows.append({**dict(r), **{f"b_{k}": ag[k] for k in BUCKETS}, "credit": ag["credit"],
                     "past_due": ag["past_due"]})
    rows.sort(key=lambda x: -x["past_due"])
    cols = [Column("property_code", "Property", link=("property", "property_id")),
            Column("unit_label", "Unit"), Column("tenants", "Tenants", link=("lease", "lease_id")),
            Column("status", "Lease"),
            Column("b_current", "Not yet due", "money"), Column("b_1_30", "1–30 days", "money"),
            Column("b_31_60", "31–60", "money"), Column("b_61_90", "61–90", "money"),
            Column("b_90_plus", "90+", "money"), Column("past_due", "Past due", "money"),
            Column("balance_cents", "Balance", "money")]
    return Report("aging", "Delinquency / aging", f"As of {today.isoformat()} · {len(rows)} leases owing",
                  cols, rows, _sum_totals(rows, cols, "property_code"),
                  ["Payments are applied oldest charge first (see Settings), so the buckets show "
                   "how long each unpaid amount has been outstanding."])


# ---- 3. Monthly collections ----------------------------------------------------------

def collections(conn: sqlite3.Connection, period: str, property_ids: list[int] | None = None) -> Report:
    start, end = period_start(period).isoformat(), period_end(period).isoformat()
    flt, params = _in("p.id", property_ids)
    props = {r["id"]: {"property_id": r["id"], "property_code": r["code"], "property_name": r["name"],
                       "billed_cents": 0, "paid_cents": 0, "received_cents": 0, "outstanding_cents": 0}
             for r in conn.execute(f"SELECT id, code, name FROM properties p WHERE p.status = 'active' AND {flt} "
                                   "ORDER BY code", params)}
    lease_prop = {r[0]: r[1] for r in conn.execute(
        "SELECT l.id, u.property_id FROM leases l JOIN units u ON u.id = l.unit_id")}
    for lid, (billed, unpaid) in ledger.period_status(conn, period).items():
        row = props.get(lease_prop.get(lid))
        if row:
            row["billed_cents"] += billed
            row["paid_cents"] += billed - unpaid
    for r in conn.execute("""
        SELECT u.property_id, SUM(pay.amount_cents) FROM payments pay JOIN leases l ON l.id = pay.lease_id
          JOIN units u ON u.id = l.unit_id
         WHERE pay.received_date BETWEEN ? AND ? AND pay.voided_at IS NULL AND pay.method <> 'deposit_applied'
         GROUP BY u.property_id""", (start, end)):
        if r[0] in props:
            props[r[0]]["received_cents"] = r[1]
    for r in conn.execute("""
        SELECT u.property_id, SUM(b.balance_cents) FROM leases l JOIN units u ON u.id = l.unit_id
          JOIN v_lease_balances b ON b.lease_id = l.id WHERE b.balance_cents > 0 GROUP BY u.property_id"""):
        if r[0] in props:
            props[r[0]]["outstanding_cents"] = r[1]
    rows = [r for r in props.values() if r["billed_cents"] or r["received_cents"] or r["outstanding_cents"]]
    for r in rows:
        r["rate"] = (100 * r["paid_cents"] / r["billed_cents"]) if r["billed_cents"] else None
    cols = [Column("property_code", "Property", link=("property", "property_id")),
            Column("property_name", "Name"), Column("billed_cents", "Billed for the month", "money"),
            Column("paid_cents", "Paid of that", "money"), Column("rate", "Collected %", "pct"),
            Column("received_cents", "Cash received in month", "money"),
            Column("outstanding_cents", "Total owed now", "money")]
    totals = _sum_totals(rows, cols, "property_code")
    totals["rate"] = (100 * totals["paid_cents"] / totals["billed_cents"]) if totals["billed_cents"] else None
    return Report("collections", "Monthly collections", f"{period} · rent and recurring charges billed for the month",
                  cols, rows, totals,
                  ["'Paid of that' counts rent paid early (before the month began) as paid. 'Cash received' is "
                   "money that arrived during the month, including payments toward older balances."])


# ---- 4. Income statement (P&L) ---------------------------------------------------------

def _pl_numbers(conn, start: str, end: str, property_ids: list[int] | None, basis: str) -> dict[str, Any]:
    pflt, pparams = _in("u.property_id", property_ids)
    income: list[tuple[str, int]] = []
    if basis == "accrual":
        for r in conn.execute(f"""
            SELECT c.charge_type, SUM(c.amount_cents) AS amt FROM charges c
              JOIN leases l ON l.id = c.lease_id JOIN units u ON u.id = l.unit_id
             WHERE c.voided_at IS NULL AND c.due_date BETWEEN ? AND ? AND c.charge_type <> 'opening_balance'
               AND {pflt}
             GROUP BY c.charge_type ORDER BY c.charge_type = 'credit', c.charge_type""", [start, end, *pparams]):
            label = "Credits and concessions" if r["charge_type"] == "credit" else r["charge_type"].replace("_", " ").capitalize()
            income.append((label, r["amt"]))
    else:
        received = conn.execute(f"""
            SELECT COALESCE(SUM(pay.amount_cents), 0) FROM payments pay
              JOIN leases l ON l.id = pay.lease_id JOIN units u ON u.id = l.unit_id
             WHERE pay.voided_at IS NULL AND pay.received_date BETWEEN ? AND ?
               AND pay.method <> 'deposit_applied' AND {pflt}""", [start, end, *pparams]).fetchone()[0]
        applied = conn.execute(f"""
            SELECT COALESCE(SUM(pay.amount_cents), 0) FROM payments pay
              JOIN leases l ON l.id = pay.lease_id JOIN units u ON u.id = l.unit_id
             WHERE pay.voided_at IS NULL AND pay.received_date BETWEEN ? AND ?
               AND pay.method = 'deposit_applied' AND {pflt}""", [start, end, *pparams]).fetchone()[0]
        income.append(("Rent and fees received", received))
        if applied:
            income.append(("Security deposits applied to unpaid rent", applied))
    kept = conn.execute(f"""
        SELECT COALESCE(SUM(d.amount_cents), 0) FROM deposit_transactions d
          JOIN leases l ON l.id = d.lease_id JOIN units u ON u.id = l.unit_id
         WHERE d.voided_at IS NULL AND d.txn_type = 'deduction' AND d.txn_date BETWEEN ? AND ? AND {pflt}""",
        [start, end, *pparams]).fetchone()[0]
    if kept:
        income.append(("Security deposits kept for damages", kept))
    eflt, eparams = _in("e.property_id", property_ids)
    expenses = conn.execute(f"""
        SELECT c.name, c.is_capital, c.tax_line, SUM(e.amount_cents) AS amt FROM expenses e
          JOIN expense_categories c ON c.id = e.category_id
         WHERE e.voided_at IS NULL AND e.expense_date BETWEEN ? AND ? AND {eflt}
         GROUP BY c.id ORDER BY c.name""", [start, end, *eparams]).fetchall()
    lflt, lparams = _in("ln.property_id", property_ids)
    interest = conn.execute(f"""
        SELECT COALESCE(SUM(lp.interest_cents), 0), COALESCE(SUM(lp.principal_cents + lp.extra_principal_cents), 0)
          FROM loan_payments lp JOIN loans ln ON ln.id = lp.loan_id
         WHERE lp.payment_date BETWEEN ? AND ? AND {lflt}""", [start, end, *lparams]).fetchone()
    return {"income": income, "operating": [(r["name"], r["amt"]) for r in expenses if not r["is_capital"]],
            "capital": [(r["name"], r["amt"]) for r in expenses if r["is_capital"]],
            "interest": interest[0], "principal": interest[1]}


def income_statement(conn: sqlite3.Connection, start: str, end: str, property_ids: list[int] | None = None,
                     basis: str = "cash") -> Report:
    n = _pl_numbers(conn, start, end, property_ids, basis)
    rows: list[dict[str, Any]] = []

    def section(title: str, lines: list[tuple[str, int]], total_label: str) -> int:
        rows.append({"line": title, "_class": "heading"})
        for label, amt in lines:
            rows.append({"line": label, "amount": amt})
        total = sum(a for _, a in lines)
        rows.append({"line": total_label, "amount": total, "_class": "subtotal"})
        return total

    income = section("Income", n["income"], "Total income")
    opex = section("Operating expenses", n["operating"], "Total operating expenses")
    noi = income - opex
    rows.append({"line": "Net operating income (NOI)", "amount": noi, "_class": "total"})
    if n["interest"]:
        rows.append({"line": "Mortgage interest", "amount": -n["interest"]})
    rows.append({"line": "Net income (before depreciation)", "amount": noi - n["interest"], "_class": "total"})
    if n["capital"]:
        section("Capital improvements (depreciated, not in net income)", n["capital"], "Total capital improvements")
    notes = [f"{'Cash' if basis == 'cash' else 'Accrual'} basis."]
    if basis == "cash":
        notes.append("Cash basis counts money when received. Security deposits are not income until kept.")
    if property_ids is None:
        notes.append("Includes portfolio overhead (expenses not assigned to a property).")
    else:
        notes.append("Overhead expenses not assigned to a property are excluded when filtering.")
    return Report("income-statement", "Income statement (P&L)", f"{start} to {end}",
                  [Column("line", "Line"), Column("amount", "Amount", "money")], rows, None, notes)


# ---- 5. Schedule E summary (US) -------------------------------------------------------

def schedule_e(conn: sqlite3.Connection, year: int, property_ids: list[int] | None = None) -> Report:
    start, end = f"{year}-01-01", f"{year}-12-31"
    flt, params = _in("p.id", property_ids)
    props = conn.execute(f"SELECT id, code, name FROM properties p WHERE {flt} ORDER BY code", params).fetchall()
    lines = [r[0] for r in conn.execute(
        "SELECT DISTINCT tax_line FROM expense_categories WHERE tax_line IS NOT NULL AND is_capital = 0")]

    def line_no(t: str) -> int:
        digits = "".join(ch for ch in t if ch.isdigit())
        return int(digits) if digits else 99

    lines.sort(key=line_no)
    rows = []
    targets = [(p["id"], p["code"], p["name"]) for p in props]
    if property_ids is None:
        targets.append((None, "Overhead", "Not assigned to a property"))
    for pid, code, name in targets:
        row: dict[str, Any] = {"property_code": code, "property_name": name, "property_id": pid}
        if pid is not None:
            nums = _pl_numbers(conn, start, end, [pid], "cash")
            row["rents"] = sum(a for _, a in nums["income"])
            row["interest"] = nums["interest"]
        else:
            row["rents"], row["interest"] = 0, 0
        pf = "e.property_id = ?" if pid is not None else "e.property_id IS NULL"
        for r in conn.execute(f"""
            SELECT c.tax_line, SUM(e.amount_cents) AS amt FROM expenses e
              JOIN expense_categories c ON c.id = e.category_id
             WHERE e.voided_at IS NULL AND c.is_capital = 0 AND c.tax_line IS NOT NULL
               AND e.expense_date BETWEEN ? AND ? AND {pf}
             GROUP BY c.tax_line""", [start, end, *([pid] if pid is not None else [])]):
            row[r["tax_line"]] = r["amt"]
        row["total_expenses"] = sum(row.get(t) or 0 for t in lines) + row["interest"]
        row["net"] = row["rents"] - row["total_expenses"]
        if row["rents"] or row["total_expenses"]:
            rows.append(row)
    used = [t for t in lines if any(r.get(t) for r in rows)]
    cols = [Column("property_code", "Property", link=("property", "property_id")), Column("rents", "Rents received (line 3)", "money")]
    cols += [Column(t, t, "money") for t in used]
    if any(r["interest"] for r in rows):
        cols.append(Column("interest", "Mortgage interest (loans)", "money"))
    cols += [Column("total_expenses", "Total expenses", "money"), Column("net", "Net before depreciation", "money")]
    return Report("schedule-e", "Schedule E summary", f"Tax year {year} · cash basis", cols, rows,
                  _sum_totals(rows, cols, "property_code"),
                  ["Depreciation (line 18) is not included. Capital improvements are excluded; give their "
                   "list to your tax preparer.", "Categories map to lines in Settings → Categories. Check "
                   "with your tax preparer."])


# ---- 6. Expense detail ---------------------------------------------------------------

def expense_detail(conn: sqlite3.Connection, start: str, end: str, property_ids: list[int] | None = None,
                   category_id: int | None = None, vendor_id: int | None = None) -> Report:
    from .expenses import list_expenses
    rows = [dict(r) for r in list_expenses(conn, start=start, end=end, property_ids=property_ids,
                                           category_id=category_id, vendor_id=vendor_id,
                                           include_voided=False, limit=100000)]
    rows.reverse()
    cols = [Column("expense_date", "Date", "date"), Column("property_code", "Property", link=("property", "property_id")),
            Column("unit_label", "Unit"), Column("category_name", "Category"), Column("vendor_name", "Vendor"),
            Column("description", "Description", link=("expense", "id")), Column("reference", "Ref"),
            Column("amount_cents", "Amount", "money")]
    return Report("expenses", "Expense detail", f"{start} to {end} · {len(rows)} expenses", cols, rows,
                  _sum_totals(rows, cols, "expense_date"))


# ---- 7. Vacancy ----------------------------------------------------------------------

def vacancy(conn: sqlite3.Connection, today: date, property_ids: list[int] | None = None) -> Report:
    flt, params = _in("r.property_id", property_ids)
    rows = [dict(r) for r in conn.execute(f"""
        SELECT r.property_id, r.property_code, r.property_name, r.unit_id, r.unit_label, r.market_rent_cents,
               (SELECT MAX(COALESCE(l.move_out_date, l.end_date)) FROM leases l
                 WHERE l.unit_id = r.unit_id AND l.status IN ('ended','terminated')) AS vacant_since,
               (SELECT MIN(l.start_date) FROM leases l WHERE l.unit_id = r.unit_id AND l.status = 'future') AS next_lease_start
          FROM v_rent_roll r WHERE r.occupancy = 'vacant' AND {flt}
         ORDER BY r.property_code, r.unit_label""", params)]
    for r in rows:
        r["days_vacant"] = (today - parse_date(r["vacant_since"])).days if r["vacant_since"] else None
        r["lost_rent_cents"] = (r["market_rent_cents"] or 0) * (r["days_vacant"] or 0) // 30
    cols = [Column("property_code", "Property", link=("property", "property_id")), Column("property_name", "Name"),
            Column("unit_label", "Unit", link=("unit", "unit_id")), Column("vacant_since", "Vacant since", "date"),
            Column("days_vacant", "Days vacant", "int"), Column("next_lease_start", "Next lease starts", "date"),
            Column("market_rent_cents", "Market rent", "money"), Column("lost_rent_cents", "Lost rent (est.)", "money")]
    return Report("vacancy", "Vacancy", f"{len(rows)} vacant units as of {today.isoformat()}", cols, rows,
                  _sum_totals(rows, cols, "property_code"),
                  ["Lost rent = market rent × days vacant ÷ 30. Units never leased in the app show no vacancy date."])


# ---- 8. Lease expirations ----------------------------------------------------------------

def lease_expirations(conn: sqlite3.Connection, today: date, months: int = 12,
                      property_ids: list[int] | None = None) -> Report:
    until = add_periods(period_of(today), months - 1)
    flt, params = _in("p.id", property_ids)
    rows = [dict(r) for r in conn.execute(f"""
        SELECT l.id AS lease_id, l.end_date, substr(l.end_date, 1, 7) AS month, p.id AS property_id,
               p.code AS property_code, u.unit_label, {TENANTS_SQL} AS tenants, cr.current_rent_cents,
               u.market_rent_cents, l.notice_given_date,
               EXISTS (SELECT 1 FROM leases f WHERE f.renewal_of_lease_id = l.id) AS renewed
          FROM leases l JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
          JOIN v_lease_current_rent cr ON cr.lease_id = l.id
         WHERE l.status = 'active' AND l.end_date IS NOT NULL AND l.end_date <= ? AND {flt}
         ORDER BY l.end_date, p.code""", [period_end(until).isoformat(), *params])]
    for r in rows:
        r["state"] = "Renewed" if r["renewed"] else ("Notice given" if r["notice_given_date"] else
                                                     ("Past end date" if r["end_date"] < today.isoformat() else "Open"))
    cols = [Column("month", "Month"), Column("end_date", "Ends", "date"),
            Column("property_code", "Property", link=("property", "property_id")), Column("unit_label", "Unit"),
            Column("tenants", "Tenants", link=("lease", "lease_id")), Column("state", "Status"),
            Column("current_rent_cents", "Rent", "money"), Column("market_rent_cents", "Market rent", "money")]
    return Report("expirations", "Lease expirations", f"Active leases ending by {until}", cols, rows)


# ---- 9. Security deposit register ---------------------------------------------------------

def deposit_register(conn: sqlite3.Connection, today: date, property_ids: list[int] | None = None) -> Report:
    flt, params = _in("p.id", property_ids)
    days = get_int_setting(conn, "deposit_return_days", 30)
    rows = [dict(r) for r in conn.execute(f"""
        SELECT l.id AS lease_id, l.status, l.move_out_date, l.deposit_cents, p.id AS property_id,
               p.code AS property_code, u.unit_label, {TENANTS_SQL} AS tenants, d.held_cents
          FROM v_deposit_held d JOIN leases l ON l.id = d.lease_id
          JOIN units u ON u.id = l.unit_id JOIN properties p ON p.id = u.property_id
         WHERE d.held_cents <> 0 AND {flt} ORDER BY p.code, u.unit_label""", params)]
    for r in rows:
        if r["move_out_date"] and r["status"] in ("ended", "terminated"):
            due = parse_date(r["move_out_date"]) + timedelta(days=days)
            r["return_due"] = due.isoformat()
            r["note"] = "OVERDUE" if due < today else "Return due"
        else:
            r["return_due"], r["note"] = None, ""
    cols = [Column("property_code", "Property", link=("property", "property_id")), Column("unit_label", "Unit"),
            Column("tenants", "Tenants", link=("lease", "lease_id")), Column("status", "Lease"),
            Column("deposit_cents", "Agreed", "money"), Column("held_cents", "Held", "money"),
            Column("return_due", "Return by", "date"), Column("note", "")]
    return Report("deposits", "Security deposit register", "Money held for tenants (a liability, not income)",
                  cols, rows, _sum_totals(rows, cols, "property_code"),
                  [f"Return deadline uses {days} days after move-out (Settings). Check your local law."])


# ---- 10. Rent vs market ----------------------------------------------------------------

def rent_vs_market(conn: sqlite3.Connection, property_ids: list[int] | None = None) -> Report:
    flt, params = _in("property_id", property_ids)
    rows = [dict(r) for r in conn.execute(f"""
        SELECT * FROM v_rent_roll WHERE occupancy = 'occupied' AND market_rent_cents IS NOT NULL
           AND current_rent_cents < market_rent_cents AND {flt}
         ORDER BY market_rent_cents - current_rent_cents DESC""", params)]
    for r in rows:
        r["gap_cents"] = r["market_rent_cents"] - r["current_rent_cents"]
        r["gap_pct"] = 100 * r["gap_cents"] / r["market_rent_cents"] if r["market_rent_cents"] else None
        r["annual_cents"] = r["gap_cents"] * 12
    cols = [Column("property_code", "Property", link=("property", "property_id")), Column("unit_label", "Unit"),
            Column("tenants", "Tenants", link=("lease", "lease_id")), Column("end_date", "Lease ends", "date"),
            Column("current_rent_cents", "Rent", "money"), Column("market_rent_cents", "Market", "money"),
            Column("gap_cents", "Below market", "money"), Column("gap_pct", "Gap %", "pct"),
            Column("annual_cents", "Per year", "money")]
    return Report("rent-vs-market", "Rent vs market", f"{len(rows)} units renting below market rent",
                  cols, rows, _sum_totals(rows, cols, "property_code"),
                  ["Set market rent on each unit to keep this report useful."])


# ---- 11. 1099 vendors (US) ----------------------------------------------------------------

def vendor_1099(conn: sqlite3.Connection, year: int, threshold_cents: int = 60000) -> Report:
    rows = [dict(r) for r in conn.execute("""
        SELECT v.id AS vendor_id, v.name, v.trade, v.tax_id_last4, v.needs_1099,
               SUM(e.amount_cents) AS paid_cents,
               SUM(CASE WHEN e.payment_method = 'card' THEN e.amount_cents ELSE 0 END) AS card_cents
          FROM expenses e JOIN vendors v ON v.id = e.vendor_id
         WHERE e.voided_at IS NULL AND e.expense_date BETWEEN ? AND ?
         GROUP BY v.id HAVING SUM(e.amount_cents) >= ?
         ORDER BY v.name COLLATE NOCASE""", (f"{year}-01-01", f"{year}-12-31", threshold_cents))]
    for r in rows:
        r["reportable_cents"] = r["paid_cents"] - r["card_cents"]
        r["flag"] = "Marked for 1099" if r["needs_1099"] else "Check if 1099 needed"
    cols = [Column("name", "Vendor", link=("vendor", "vendor_id")), Column("trade", "Trade"),
            Column("tax_id_last4", "Tax ID (last 4)"), Column("paid_cents", "Paid", "money"),
            Column("card_cents", "Paid by card", "money"), Column("reportable_cents", "Paid other ways", "money"),
            Column("flag", "")]
    return Report("1099", "1099 vendor summary", f"{year} · vendors paid {threshold_cents // 100:,} dollars or more",
                  cols, rows, _sum_totals(rows, cols, "name"),
                  ["Card payments are usually reported by the card processor, not by you. Confirm the "
                   "current threshold and rules with your tax preparer."])


# ---- 12. Property performance --------------------------------------------------------------

def property_performance(conn: sqlite3.Connection, start: str, end: str,
                         property_ids: list[int] | None = None) -> Report:
    flt, params = _in("p.id", property_ids)
    days = (parse_date(end) - parse_date(start)).days + 1
    annualize = 365 / days if days > 0 else 1
    rows = []
    for p in conn.execute(f"SELECT * FROM properties p WHERE p.status = 'active' AND {flt} ORDER BY code", params).fetchall():
        n = _pl_numbers(conn, start, end, [p["id"]], "cash")
        income = sum(a for _, a in n["income"])
        opex = sum(a for _, a in n["operating"])
        noi = income - opex
        cash_flow = noi - n["interest"] - n["principal"] - sum(a for _, a in n["capital"])
        row = {"property_id": p["id"], "property_code": p["code"], "property_name": p["name"],
               "income": income, "opex": opex, "noi": noi, "cash_flow": cash_flow,
               "expense_ratio": 100 * opex / income if income else None,
               "value": p["estimated_value_cents"],
               "cap_rate": 100 * noi * annualize / p["estimated_value_cents"] if p["estimated_value_cents"] else None,
               "coc": 100 * cash_flow * annualize / p["cash_invested_cents"] if p["cash_invested_cents"] else None}
        rows.append(row)
    cols = [Column("property_code", "Property", link=("property", "property_id")), Column("property_name", "Name"),
            Column("income", "Income", "money"), Column("opex", "Operating exp.", "money"),
            Column("noi", "NOI", "money"), Column("expense_ratio", "Expense ratio", "pct"),
            Column("cash_flow", "Cash flow", "money"), Column("value", "Est. value", "money"),
            Column("cap_rate", "Cap rate (annualized)", "pct"), Column("coc", "Cash-on-cash (annualized)", "pct")]
    totals = _sum_totals(rows, cols, "property_code")
    totals["expense_ratio"] = 100 * totals["opex"] / totals["income"] if totals["income"] else None
    totals["value"] = sum(r["value"] or 0 for r in rows) or None
    return Report("performance", "Property performance", f"{start} to {end} · cash basis", cols, rows, totals,
                  ["NOI = income − operating expenses. Cash flow also subtracts loan payments and capital improvements.",
                   "Cap rate and cash-on-cash need the estimated value and cash invested on each property."])


REPORTS = {
    "rent-roll": ("Rent roll", "Who lives where, rent, deposit and balance"),
    "aging": ("Delinquency / aging", "Who owes what, by days past due"),
    "collections": ("Monthly collections", "Billed vs collected per property"),
    "income-statement": ("Income statement (P&L)", "Income, expenses and NOI"),
    "schedule-e": ("Schedule E summary", "Per-property tax totals (US)"),
    "expenses": ("Expense detail", "Every expense, filterable"),
    "vacancy": ("Vacancy", "Vacant units, days vacant, lost rent"),
    "expirations": ("Lease expirations", "Leases ending in the next 12 months"),
    "deposits": ("Security deposit register", "Deposits held and return deadlines"),
    "rent-vs-market": ("Rent vs market", "Units renting below market"),
    "performance": ("Property performance", "NOI, cap rate, cash-on-cash"),
    "1099": ("1099 vendor summary", "Vendors paid above the threshold (US)"),
}
