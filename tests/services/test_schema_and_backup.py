import sqlite3
from datetime import datetime

import pytest

from rental_tracker import db as dbmod
from rental_tracker.services import backup, ledger
from rental_tracker.services.common import ServiceError
from tests.conftest import make_lease


def test_migrate_is_idempotent(conn):
    assert dbmod.schema_version(conn) == dbmod.latest_version()
    assert dbmod.migrate(conn) == 0


def test_newer_database_refused(conn):
    conn.execute(f"PRAGMA user_version = {dbmod.latest_version() + 1}")
    with pytest.raises(dbmod.NewerDatabaseError):
        dbmod.migrate(conn)


def test_constraints(conn, owner_id):
    lid = make_lease(conn, owner_id)
    unit = conn.execute("SELECT unit_id FROM leases WHERE id = ?", (lid,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO leases(unit_id, status, start_date, rent_cents) VALUES (?, 'active', '2026-02-01', 1)",
                     (unit,))


def test_backup_restore_round_trip(conn, data_dir, owner_id):
    lid = make_lease(conn, owner_id)
    path = backup.create_backup(conn, data_dir, "snapshots", "test")
    ledger.record_payment(conn, lid, 5000, "2026-01-02", "cash")
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    safety = backup.restore_backup(conn, data_dir, path)
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
    assert safety.exists() and "pre-restore" in safety.name
    assert not path.with_suffix(".db-wal").exists()


def test_restore_rejects_garbage(conn, data_dir):
    bad = data_dir.backups / "snapshots" / "rental-bad.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(ServiceError):
        backup.restore_backup(conn, data_dir, bad)


def test_scheduled_backups_and_rotation(conn, data_dir, tmp_path):
    ext = tmp_path / "usb"
    ext.mkdir()
    conn.execute("UPDATE settings SET value = ? WHERE key = 'backup_external_path'", (str(ext),))
    made = backup.run_scheduled_backups(conn, data_dir, datetime(2026, 3, 1, 9))
    assert {p.parent.name for p in made} == {"daily", "weekly", "monthly"}
    assert backup.run_scheduled_backups(conn, data_dir, datetime(2026, 3, 1, 18)) == []  # once a day
    for day in range(2, 20):
        backup.run_scheduled_backups(conn, data_dir, datetime(2026, 3, day, 9))
    daily = [b for b in backup.list_backups(data_dir) if b.kind == "daily"]
    assert len(daily) <= 14
    assert list((ext / "RentalTrackerBackups" / "db").glob("*.db"))


def test_upgrade_from_version_1_allows_deletes(data_dir):
    conn = dbmod.connect(data_dir.db)
    first = dbmod.migrations()[0][1].read_text(encoding="utf-8")
    conn.executescript(f"BEGIN;\n{first}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute("INSERT INTO owners(name) VALUES ('x')")
    backed_up = []
    assert dbmod.migrate(conn, before=lambda: backed_up.append(True)) == dbmod.latest_version() - 1
    assert backed_up == [True]  # existing data is backed up before upgrading
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%no_delete'").fetchall()
    conn.close()


def test_upgrade_to_version_3_keeps_payments(data_dir):
    """0003 rebuilds the payments table (for GCash and 'Other' text); nothing may be lost or unlinked."""
    conn = dbmod.connect(data_dir.db)
    for version, path in dbmod.migrations()[:2]:
        conn.executescript(f"BEGIN;\n{path.read_text(encoding='utf-8')}\nPRAGMA user_version = {version};\nCOMMIT;")
    owner = conn.execute("INSERT INTO owners(name) VALUES ('x')").lastrowid
    lid = make_lease(conn, owner, rent=100000)
    pay = conn.execute("INSERT INTO payments(lease_id, received_date, amount_cents, method, reference, receipt_number) "
                       "VALUES (?, '2026-01-03', 40000, 'money_order', '77', 'R-1')", (lid,)).lastrowid
    conn.execute("INSERT INTO deposit_transactions(lease_id, txn_date, txn_type, amount_cents, payment_id) "
                 "VALUES (?, '2026-01-03', 'applied_to_balance', 40000, ?)", (lid, pay))
    balance = conn.execute("SELECT balance_cents FROM v_lease_balances WHERE lease_id = ?", (lid,)).fetchone()[0]
    assert dbmod.migrate(conn) == dbmod.latest_version() - 2
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (pay,)).fetchone()
    assert (row["method"], row["reference"], row["receipt_number"], row["method_other"]) == ("money_order", "77", "R-1", None)
    assert conn.execute("SELECT balance_cents FROM v_lease_balances WHERE lease_id = ?", (lid,)).fetchone()[0] == balance
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert conn.execute("SELECT value FROM settings WHERE key = 'currency'").fetchone()[0] == "PHP"
    ledger.record_payment(conn, lid, 100, "2026-01-04", "gcash")
    with pytest.raises(sqlite3.IntegrityError):  # foreign keys are enforced again
        conn.execute("INSERT INTO payments(lease_id, received_date, amount_cents, method) VALUES (999, '2026-01-04', 1, 'cash')")
    conn.close()
