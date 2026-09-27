"""Numbers and alerts for the dashboard (BLUEPRINT §8, 'Dashboard and alerts')."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..config import DataDir
from ..domain.allocation import aging
from ..domain.periods import period_end, period_of, period_start
from . import backup, late_fees, ledger, reports
from .common import get_int_setting, get_setting


@dataclass
class Alert:
    level: str  # 'warn' | 'info' | 'bad'
    text: str
    link: tuple[str, dict] | None = None  # (endpoint, kwargs)


def build(conn: sqlite3.Connection, today: date, data: DataDir) -> dict:
    period = period_of(today)
    occ = conn.execute("SELECT COUNT(*) AS units, SUM(occupancy = 'occupied') AS occupied FROM v_rent_roll").fetchone()
    units, occupied = occ["units"] or 0, occ["occupied"] or 0
    coll = reports.collections(conn, period)
    expected = coll.totals["billed_cents"] if coll.totals else 0
    collected = coll.totals["paid_cents"] if coll.totals else 0
    received = coll.totals["received_cents"] if coll.totals else 0

    owing = conn.execute("SELECT lease_id FROM v_lease_balances WHERE balance_cents > 0").fetchall()
    ledgers = ledger.load_ledgers(conn, [r[0] for r in owing])
    order = ledger.payment_order(conn)
    past_due_total, delinquent = 0, 0
    for charges, payments in ledgers.values():
        ag = aging(charges, payments, today, order)
        if ag["past_due"] > 0:
            delinquent += 1
            past_due_total += ag["past_due"]
    outstanding = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) FROM v_lease_balances "
                               "WHERE balance_cents > 0").fetchone()[0]

    def expiring(days: int) -> int:
        return conn.execute("SELECT COUNT(*) FROM leases WHERE status = 'active' AND end_date BETWEEN ? AND ? "
                            "AND notice_given_date IS NULL AND id NOT IN (SELECT renewal_of_lease_id FROM leases "
                            "WHERE renewal_of_lease_id IS NOT NULL)",
                            (today.isoformat(), (today + timedelta(days=days)).isoformat())).fetchone()[0]

    candidates = late_fees.find_candidates(conn, today)
    deposits_held = conn.execute("SELECT COALESCE(SUM(held_cents), 0) FROM v_deposit_held").fetchone()[0]

    alerts: list[Alert] = []
    if candidates:
        alerts.append(Alert("warn", f"{len(candidates)} late fee{'s' if len(candidates) != 1 else ''} awaiting review",
                            ("rentday.late_fees", {})))
    days = get_int_setting(conn, "deposit_return_days", 30)
    for r in conn.execute("""
        SELECT l.id, l.move_out_date, p.code, u.unit_label, d.held_cents FROM leases l
          JOIN v_deposit_held d ON d.lease_id = l.id JOIN units u ON u.id = l.unit_id
          JOIN properties p ON p.id = u.property_id
         WHERE l.status IN ('ended','terminated') AND d.held_cents > 0 AND l.move_out_date IS NOT NULL
         ORDER BY l.move_out_date""").fetchall():
        due = date.fromisoformat(r["move_out_date"]) + timedelta(days=days)
        left = (due - today).days
        level = "bad" if left < 0 else "warn"
        when = f"{-left} days overdue" if left < 0 else f"due in {left} days ({due.isoformat()})"
        alerts.append(Alert(level, f"Deposit for {r['code']} · {r['unit_label']} must be returned — {when}",
                            ("leases.detail", {"lease_id": r["id"]})))
    for r in conn.execute("""
        SELECT l.id, l.end_date, p.code, u.unit_label FROM leases l JOIN units u ON u.id = l.unit_id
          JOIN properties p ON p.id = u.property_id
         WHERE l.status = 'active' AND l.notice_given_date IS NOT NULL AND l.end_date < ?""", (today.isoformat(),)):
        alerts.append(Alert("warn", f"{r['code']} · {r['unit_label']}: move-out date {r['end_date']} has passed — record the move-out",
                            ("leases.detail", {"lease_id": r["id"]})))
    for r in conn.execute("""
        SELECT l.id, l.start_date, p.code, u.unit_label FROM leases l JOIN units u ON u.id = l.unit_id
          JOIN properties p ON p.id = u.property_id
         WHERE l.status = 'future' AND l.start_date <= ? ORDER BY l.start_date""",
                          ((today + timedelta(days=14)).isoformat(),)):
        alerts.append(Alert("info", f"New lease starts {r['start_date']} at {r['code']} · {r['unit_label']}",
                            ("leases.detail", {"lease_id": r["id"]})))
    soon = (today + timedelta(days=30)).isoformat()
    for r in conn.execute("SELECT id, related_type, related_id, title, expires_on FROM documents "
                          "WHERE expires_on IS NOT NULL AND expires_on <= ? ORDER BY expires_on", (soon,)):
        alerts.append(Alert("bad" if r["expires_on"] < today.isoformat() else "warn",
                            f"Document '{r['title']}' expires {r['expires_on']}", None))
    for r in conn.execute("SELECT id, name, insurance_expires FROM vendors WHERE is_active = 1 AND "
                          "insurance_expires IS NOT NULL AND insurance_expires <= ?", (soon,)):
        alerts.append(Alert("warn", f"Vendor {r['name']}: insurance expires {r['insurance_expires']}",
                            ("expenses.vendor_detail", {"vendor_id": r["id"]})))
    drafts = conn.execute("SELECT COUNT(*) FROM leases WHERE status = 'draft'").fetchone()[0]
    if drafts:
        alerts.append(Alert("info", f"{drafts} draft lease{'s' if drafts != 1 else ''} not yet signed",
                            ("leases.index", {"status": "draft"})))

    backups = backup.list_backups(data)
    last_backup = backups[0].created if backups else None
    external = get_setting(conn, "backup_external_path").strip()
    last_external = get_setting(conn, "last_external_backup")
    if external:
        stale = not last_external or (datetime.now() - datetime.fromisoformat(last_external)).days >= 7
        if stale:
            alerts.append(Alert("warn", "Your external backup drive hasn't been connected for 7+ days",
                                ("admin.backups", {})))
    else:
        alerts.append(Alert("info", "Set an external backup drive so a copy survives if this computer fails",
                            ("admin.backups", {})))

    low = sorted((r for r in coll.rows if r["billed_cents"]), key=lambda r: (r["rate"] or 0, r["property_code"]))[:8]
    vac = reports.vacancy(conn, today)
    return {
        "period": period, "period_start": period_start(period), "period_end": period_end(period),
        "units": units, "occupied": occupied,
        "occupancy_pct": 100 * occupied / units if units else None,
        "expected": expected, "collected": collected, "received": received,
        "collected_pct": 100 * collected / expected if expected else None,
        "delinquent": delinquent, "past_due_total": past_due_total, "outstanding": outstanding,
        "vacant": vac.rows[:8], "vacant_count": len(vac.rows),
        "expiring": {d: expiring(d) for d in (30, 60, 90)},
        "late_fee_count": len(candidates), "deposits_held": deposits_held,
        "alerts": alerts, "low_collections": low,
        "last_backup": last_backup, "last_external": last_external,
        "property_count": conn.execute("SELECT COUNT(*) FROM properties WHERE status = 'active'").fetchone()[0],
    }
