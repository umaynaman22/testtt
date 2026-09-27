"""Bulk onboarding from CSV files (BLUEPRINT §12).

Every import runs inside one transaction. A dry run does all the work and then
rolls back, so it catches every problem (including references between files)
without saving anything. A real import commits only if there were no errors.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..domain.money import parse_money
from ..domain.periods import parse_date, period_start, period_of
from . import expenses, leases, portfolio, search, tenants
from .common import ServiceError, audit, ensure_open

# Order matters: later files refer to earlier ones.
KINDS = ("owners", "properties", "units", "tenants", "vendors", "leases", "opening_balances")

TEMPLATES: dict[str, dict[str, Any]] = {
    "owners": {"columns": ["name*", "entity_type", "email", "phone", "mailing_address"],
               "example": ["Sakai Holdings LLC", "llc", "office@example.com", "555-0100", "PO Box 1, Springfield"]},
    "properties": {"columns": ["code*", "owner_name*", "name*", "property_type*", "address_line1*", "address_line2",
                               "city*", "state*", "postal_code*", "year_built", "purchase_date", "purchase_price",
                               "estimated_value", "tags"],
                   "example": ["MAPLE-12", "Sakai Holdings LLC", "12 Maple St", "single_family", "12 Maple St", "",
                               "Springfield", "IL", "62701", "1978", "2019-06-14", "185000", "240000",
                               "North side; Section 8"]},
    "units": {"columns": ["property_code*", "unit_label*", "bedrooms", "bathrooms", "square_feet", "market_rent"],
              "example": ["MAPLE-12", "Main", "3", "1.5", "1350", "1650"]},
    "tenants": {"columns": ["tenant_key*", "first_name*", "last_name*", "email", "phone"],
                "example": ["T-0001", "Ann", "Lee", "ann@example.com", "555-0101"]},
    "vendors": {"columns": ["name*", "trade", "phone", "email", "needs_1099"],
                "example": ["Ace Plumbing", "plumber", "555-0199", "ace@example.com", "yes"]},
    "leases": {"columns": ["property_code*", "unit_label", "tenant_keys*", "start_date*", "end_date", "rent*",
                           "due_day", "deposit", "late_fee_type", "late_fee_amount", "grace_days", "billing_start"],
               "example": ["MAPLE-12", "Main", "T-0001; T-0002", "2024-08-01", "2025-07-31", "1550", "1", "1550",
                           "flat", "50", "5", ""]},
    "opening_balances": {"columns": ["property_code*", "unit_label", "balance*", "deposit_held*", "as_of_date*"],
                         "example": ["MAPLE-12", "Main", "125.00", "1550", "2026-10-01"]},
}

HELP = {
    "leases": "unit_label can be blank for single-unit properties. tenant_keys: separate with ';' — the first "
              "is the primary tenant. late_fee_type: none, flat or percent (late_fee_amount is dollars or a "
              "percentage). billing_start: the first day this app bills rent (default: the 1st of this month), "
              "so years of old rent are not billed again.",
    "opening_balances": "balance = what the tenant owed before the billing start month's rent (negative = "
                        "credit). deposit_held = security deposit you currently hold. as_of_date should be the "
                        "day before billing starts, or the billing start date.",
    "properties": "property_type: single_family, multi_family, condo, townhouse, mobile_home, commercial, "
                  "mixed_use or other. Properties with no rows in units.csv get one unit called 'Main'. "
                  "tags: separate with ';'.",
}


def template_csv(kind: str) -> str:
    t = TEMPLATES[kind]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c.rstrip("*") for c in t["columns"]])
    w.writerow(t["example"])
    return buf.getvalue()


@dataclass
class RowError:
    file: str
    row: int  # spreadsheet row number (header = 1)
    message: str


@dataclass
class ImportResult:
    committed: bool = False
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def bump(self, kind: str, what: str) -> None:
        self.counts.setdefault(kind, {"created": 0, "updated": 0, "skipped": 0})[what] += 1


def decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ServiceError("Could not read the file. Save it as CSV (UTF-8) and try again.")


def _norm(header: str) -> str:
    return header.strip().lower().rstrip("*").strip().replace(" ", "_").replace("-", "_")


class _Row:
    def __init__(self, data: dict[str, str]):
        self.d = {k: (v or "").strip() for k, v in data.items() if k}

    def s(self, key: str) -> str | None:
        return self.d.get(key) or None

    def req(self, key: str) -> str:
        v = self.s(key)
        if v is None:
            raise ServiceError(f"{key} is required")
        return v

    def money(self, key: str, required: bool = False) -> int | None:
        v = self.req(key) if required else self.s(key)
        if v is None:
            return None
        try:
            return parse_money(v)
        except ValueError as e:
            raise ServiceError(f"{key}: {e}") from None

    def int(self, key: str) -> int | None:
        v = self.s(key)
        if v is None:
            return None
        try:
            return int(float(v))
        except ValueError:
            raise ServiceError(f"{key}: not a whole number: {v!r}") from None

    def num(self, key: str) -> float | None:
        v = self.s(key)
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            raise ServiceError(f"{key}: not a number: {v!r}") from None

    def date(self, key: str, required: bool = False) -> str | None:
        v = self.req(key) if required else self.s(key)
        if v is None:
            return None
        try:
            return parse_date(v).isoformat()
        except ValueError as e:
            raise ServiceError(f"{key}: {e}") from None

    def enum(self, key: str) -> str | None:
        v = self.s(key)
        return v.lower().replace(" ", "_").replace("-", "_") if v else None

    def yes(self, key: str) -> int:
        return int((self.s(key) or "").lower() in ("y", "yes", "true", "1", "x"))


def _find_unit(conn, code: str, label: str | None) -> sqlite3.Row:
    prop = conn.execute("SELECT id FROM properties WHERE code = ?", (code.upper(),)).fetchone()
    if not prop:
        raise ServiceError(f"unknown property_code {code!r}")
    if label:
        unit = conn.execute("SELECT * FROM units WHERE property_id = ? AND unit_label = ?", (prop["id"], label)).fetchone()
        if not unit:
            raise ServiceError(f"property {code} has no unit {label!r}")
        return unit
    units = conn.execute("SELECT * FROM units WHERE property_id = ? AND status = 'active'", (prop["id"],)).fetchall()
    if len(units) != 1:
        raise ServiceError(f"property {code} has {len(units)} units — fill in unit_label")
    return units[0]


def run_import(conn: sqlite3.Connection, files: dict[str, str], *, commit: bool, today: date) -> ImportResult:
    result = ImportResult()
    parsed: dict[str, list[_Row]] = {}
    for kind in KINDS:
        text = files.get(kind)
        if not text or not text.strip():
            continue
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            result.errors.append(RowError(kind, 1, "the file has no header row"))
            continue
        reader.fieldnames = [_norm(h) for h in reader.fieldnames]
        known = {c.rstrip("*") for c in TEMPLATES[kind]["columns"]}
        required = {c.rstrip("*") for c in TEMPLATES[kind]["columns"] if c.endswith("*")}
        missing = required - set(reader.fieldnames)
        if missing:
            result.errors.append(RowError(kind, 1, "missing column(s): " + ", ".join(sorted(missing))))
            continue
        extra = [h for h in reader.fieldnames if h and h not in known]
        if extra:
            result.warnings.append(f"{kind}.csv: ignored unknown column(s): {', '.join(extra)}")
        parsed[kind] = [_Row(r) for r in reader]
    if not parsed:
        if not result.errors:
            result.errors.append(RowError("-", 0, "Choose at least one file to import"))
        return result
    if result.errors:
        return result

    conn.execute("BEGIN IMMEDIATE")
    new_properties: list[int] = []
    tenant_keys: dict[str, int] = {}
    try:
        handlers = {
            "owners": _owner, "properties": lambda c, r, res: new_properties.extend(_property(c, r, res)),
            "units": _unit, "tenants": lambda c, r, res: _tenant(c, r, res, tenant_keys),
            "vendors": _vendor, "leases": lambda c, r, res: _lease(c, r, res, today),
            "opening_balances": _opening,
        }
        for kind in KINDS:
            for i, row in enumerate(parsed.get(kind, []), start=2):
                if not any(row.d.values()):
                    continue
                conn.execute("SAVEPOINT import_row")
                try:
                    handlers[kind](conn, row, result)
                    conn.execute("RELEASE import_row")
                except (ServiceError, ValueError, sqlite3.IntegrityError) as e:
                    conn.execute("ROLLBACK TO import_row")
                    conn.execute("RELEASE import_row")
                    result.errors.append(RowError(kind, i, str(e)))
            if kind == "units":
                _default_units(conn, new_properties)
        if "units" not in parsed:
            _default_units(conn, new_properties)
        search.rebuild(conn)
        if commit and not result.errors:
            audit(conn, "import", changes={k: v for k, v in result.counts.items()})
            conn.execute("COMMIT")
            result.committed = True
        else:
            conn.execute("ROLLBACK")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return result


def _default_units(conn, property_ids: list[int]) -> None:
    for pid in property_ids:
        if not conn.execute("SELECT 1 FROM units WHERE property_id = ?", (pid,)).fetchone():
            portfolio.save_unit(conn, None, pid, {"unit_label": "Main"}, reindex=False)


def _owner(conn, row: _Row, res: ImportResult) -> None:
    name = row.req("name")
    existing = conn.execute("SELECT id FROM owners WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    fields = {"name": name, "entity_type": row.enum("entity_type") or "individual", "email": row.s("email"),
              "phone": row.s("phone"), "mailing_address": row.s("mailing_address")}
    if fields["entity_type"] not in portfolio.ENTITY_TYPES:
        raise ServiceError(f"entity_type must be one of: {', '.join(portfolio.ENTITY_TYPES)}")
    if existing:
        portfolio.save_owner(conn, existing["id"], {k: v for k, v in fields.items() if v}, reindex=False)
        res.bump("owners", "updated")
    else:
        portfolio.save_owner(conn, None, fields, reindex=False)
        res.bump("owners", "created")


def _property(conn, row: _Row, res: ImportResult) -> list[int]:
    code = row.req("code").upper()
    owner_name = row.req("owner_name")
    owner = conn.execute("SELECT id FROM owners WHERE name = ? COLLATE NOCASE", (owner_name,)).fetchone()
    if not owner:
        raise ServiceError(f"unknown owner_name {owner_name!r} — add it to owners.csv")
    ptype = row.enum("property_type")
    if ptype not in portfolio.PROPERTY_TYPES:
        raise ServiceError(f"property_type must be one of: {', '.join(portfolio.PROPERTY_TYPES)}")
    fields = {"owner_id": owner["id"], "code": code, "name": row.req("name"), "property_type": ptype,
              "address_line1": row.req("address_line1"), "address_line2": row.s("address_line2"),
              "city": row.req("city"), "state": row.req("state"), "postal_code": row.req("postal_code"),
              "year_built": row.int("year_built"), "purchase_date": row.date("purchase_date"),
              "purchase_price_cents": row.money("purchase_price"),
              "estimated_value_cents": row.money("estimated_value")}
    if fields["estimated_value_cents"]:
        fields["value_as_of"] = date.today().isoformat()
    tags = [t for t in (row.s("tags") or "").split(";") if t.strip()]
    existing = conn.execute("SELECT * FROM properties WHERE code = ?", (code,)).fetchone()
    if existing:
        merged = {**{k: existing[k] for k in portfolio.PROPERTY_FIELDS},
                  **{k: v for k, v in fields.items() if v is not None}}
        portfolio.save_property(conn, existing["id"], merged, tags=tags or None, reindex=False)
        res.bump("properties", "updated")
        return []
    pid = portfolio.save_property(conn, None, fields, tags=tags, unit_labels=[], reindex=False)
    res.bump("properties", "created")
    return [pid]


def _unit(conn, row: _Row, res: ImportResult) -> None:
    code = row.req("property_code").upper()
    prop = conn.execute("SELECT id FROM properties WHERE code = ?", (code,)).fetchone()
    if not prop:
        raise ServiceError(f"unknown property_code {code!r}")
    label = row.req("unit_label")
    fields = {"unit_label": label, "bedrooms": row.num("bedrooms"), "bathrooms": row.num("bathrooms"),
              "square_feet": row.int("square_feet"), "market_rent_cents": row.money("market_rent")}
    existing = conn.execute("SELECT * FROM units WHERE property_id = ? AND unit_label = ?", (prop["id"], label)).fetchone()
    if existing:
        merged = {**{k: existing[k] for k in portfolio.UNIT_FIELDS}, **{k: v for k, v in fields.items() if v is not None}}
        portfolio.save_unit(conn, existing["id"], prop["id"], merged, reindex=False)
        res.bump("units", "updated")
    else:
        portfolio.save_unit(conn, None, prop["id"], fields, reindex=False)
        res.bump("units", "created")


def _tenant(conn, row: _Row, res: ImportResult, keys: dict[str, int]) -> None:
    key = row.req("tenant_key")
    fields = {"external_ref": key, "first_name": row.req("first_name"), "last_name": row.req("last_name"),
              "email": row.s("email"), "phone": row.s("phone")}
    existing = conn.execute("SELECT * FROM tenants WHERE external_ref = ?", (key,)).fetchone()
    if existing:
        merged = {**{k: existing[k] for k in tenants.FIELDS}, **{k: v for k, v in fields.items() if v}}
        keys[key] = tenants.save_tenant(conn, existing["id"], merged, reindex=False)
        res.bump("tenants", "updated")
    else:
        keys[key] = tenants.save_tenant(conn, None, fields, reindex=False)
        res.bump("tenants", "created")


def _vendor(conn, row: _Row, res: ImportResult) -> None:
    name = row.req("name")
    fields = {"name": name, "trade": row.s("trade"), "phone": row.s("phone"), "email": row.s("email"),
              "needs_1099": row.yes("needs_1099")}
    existing = conn.execute("SELECT * FROM vendors WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if existing:
        merged = {**{k: existing[k] for k in expenses.VENDOR_FIELDS}, **{k: v for k, v in fields.items() if v}}
        expenses.save_vendor(conn, existing["id"], merged, reindex=False)
        res.bump("vendors", "updated")
    else:
        expenses.save_vendor(conn, None, fields, reindex=False)
        res.bump("vendors", "created")


def _lease(conn, row: _Row, res: ImportResult, today: date) -> None:
    unit = _find_unit(conn, row.req("property_code"), row.s("unit_label"))
    start = row.date("start_date", required=True)
    if conn.execute("SELECT 1 FROM leases WHERE unit_id = ? AND start_date = ?", (unit["id"], start)).fetchone():
        res.bump("leases", "skipped")
        return
    tenant_ids = []
    for i, key in enumerate(k.strip() for k in row.req("tenant_keys").split(";") if k.strip()):
        t = conn.execute("SELECT id FROM tenants WHERE external_ref = ?", (key,)).fetchone()
        if not t:
            raise ServiceError(f"unknown tenant_key {key!r} — add it to tenants.csv")
        tenant_ids.append((t["id"], "primary" if i == 0 else "co_tenant"))
    end = row.date("end_date")
    fee_type = row.enum("late_fee_type") or "none"
    if fee_type not in leases.LATE_FEE_TYPES:
        raise ServiceError("late_fee_type must be none, flat or percent")
    terms: dict[str, Any] = {"rent_due_day": row.int("due_day") or 1, "deposit_cents": row.money("deposit") or 0,
                             "late_fee_type": fee_type, "late_fee_grace_days": row.int("grace_days") or 5}
    if fee_type == "flat":
        terms["late_fee_flat_cents"] = row.money("late_fee_amount", required=True)
    elif fee_type == "percent":
        terms["late_fee_percent_bp"] = round((row.num("late_fee_amount") or 0) * 100)
    billing = row.date("billing_start") or max(start, period_start(period_of(today)).isoformat())
    lease_id = leases.create_lease(conn, unit_id=unit["id"], tenants=tenant_ids, start=start, end=end,
                                   rent_cents=row.money("rent", required=True), today=today,
                                   billing_start=billing, **terms)
    if end and end < today.isoformat() and start <= today.isoformat():
        conn.execute("UPDATE leases SET status = 'month_to_month' WHERE id = ?", (lease_id,))
    res.bump("leases", "created")


def _opening(conn, row: _Row, res: ImportResult) -> None:
    unit = _find_unit(conn, row.req("property_code"), row.s("unit_label"))
    lease = conn.execute("SELECT * FROM leases WHERE unit_id = ? AND status IN ('active','month_to_month')",
                         (unit["id"],)).fetchone()
    if not lease:
        raise ServiceError("no current lease on this unit — import leases.csv first")
    if conn.execute("SELECT 1 FROM charges WHERE lease_id = ? AND charge_type = 'opening_balance'",
                    (lease["id"],)).fetchone():
        raise ServiceError("this lease already has an opening balance")
    as_of = row.date("as_of_date", required=True)
    ensure_open(conn, as_of)
    balance = row.money("balance", required=True)
    held = row.money("deposit_held", required=True)
    if held < 0:
        raise ServiceError("deposit_held cannot be negative")
    if balance:
        conn.execute("INSERT INTO charges(lease_id, charge_type, source, description, amount_cents, due_date) "
                     "VALUES (?, 'opening_balance', 'import', 'Opening balance', ?, ?)", (lease["id"], balance, as_of))
    if held:
        conn.execute("INSERT INTO deposit_transactions(lease_id, txn_date, txn_type, amount_cents, description) "
                     "VALUES (?, ?, 'received', ?, 'Opening deposit balance')", (lease["id"], as_of, held))
    res.bump("opening_balances", "created")
