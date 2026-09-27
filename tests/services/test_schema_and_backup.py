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
    pid = ledger.record_payment(conn, lid, 5000, "2026-01-02", "cash")
    with pytest.raises(sqlite3.IntegrityError, match="void them instead"):
        conn.execute("DELETE FROM payments WHERE id = ?", (pid,))
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
