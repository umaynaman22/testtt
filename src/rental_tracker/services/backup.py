"""Backups, rotation and restore (BLUEPRINT §11.2).

Backups use SQLite's online backup API, so they are consistent even while the
app is running. Restores copy a backup *into* the live database the same way,
so no file swapping or restart is needed.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import db as dbmod
from ..config import DataDir
from .common import ServiceError, audit, get_int_setting, get_setting, set_setting

KINDS = ("daily", "weekly", "monthly", "snapshots")
SNAPSHOTS_KEPT = 30


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    kind: str
    created: datetime
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name


def _unique_path(folder: Path, stem: str) -> Path:
    path = folder / f"{stem}.db"
    n = 2
    while path.exists():
        path = folder / f"{stem}-{n}.db"
        n += 1
    return path


def create_backup(conn: sqlite3.Connection, data: DataDir, kind: str = "snapshots",
                  label: str = "manual", now: datetime | None = None) -> Path:
    if kind not in KINDS:
        raise ValueError(kind)
    now = now or datetime.now()
    folder = data.backups / kind
    folder.mkdir(parents=True, exist_ok=True)
    final = _unique_path(folder, f"rental-{now:%Y%m%d-%H%M%S}-{label}")
    tmp = final.with_suffix(".tmp")
    dst = sqlite3.connect(str(tmp))
    try:
        conn.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")  # self-contained file, no -wal sidecar
    finally:
        dst.close()
    tmp.replace(final)
    return final


def _created(path: Path, mtime: float) -> datetime:
    """Backup time from the file name (rental-YYYYMMDD-HHMMSS-...), else the file time."""
    try:
        return datetime.strptime(path.name[7:22], "%Y%m%d-%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(mtime)


def list_backups(data: DataDir) -> list[BackupInfo]:
    out = []
    for kind in KINDS:
        folder = data.backups / kind
        if not folder.is_dir():
            continue
        for p in folder.glob("rental-*.db"):
            st = p.stat()
            out.append(BackupInfo(p, kind, _created(p, st.st_mtime), st.st_size))
    return sorted(out, key=lambda b: b.created, reverse=True)


def _newest(data: DataDir, kind: str) -> BackupInfo | None:
    items = [b for b in list_backups(data) if b.kind == kind]
    return items[0] if items else None


def rotate(data: DataDir, keep: dict[str, int]) -> int:
    removed = 0
    for kind, n in keep.items():
        items = [b for b in list_backups(data) if b.kind == kind]
        for b in items[max(n, 1):]:
            b.path.unlink(missing_ok=True)
            removed += 1
    return removed


def sync_documents(src: Path, dst: Path) -> int:
    """Copy documents that are not in dst yet. Documents never change once stored."""
    copied = 0
    if not src.is_dir():
        return 0
    for f in src.rglob("*"):
        if f.is_file():
            target = dst / f.relative_to(src)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                copied += 1
    return copied


def run_scheduled_backups(conn: sqlite3.Connection, data: DataDir,
                          now: datetime | None = None) -> list[Path]:
    """Daily/weekly/monthly backups (grandfather-father-son) and the external copy."""
    now = now or datetime.now()
    made: list[Path] = []
    daily = _newest(data, "daily")
    if daily is None or daily.created.date() != now.date():
        path = create_backup(conn, data, "daily", "daily", now)
        made.append(path)
        weekly = _newest(data, "weekly")
        if weekly is None or (now - weekly.created).days >= 7:
            made.append(_copy_as(path, data, "weekly", now))
        monthly = _newest(data, "monthly")
        if monthly is None or (monthly.created.year, monthly.created.month) != (now.year, now.month):
            made.append(_copy_as(path, data, "monthly", now))
    rotate(data, {
        "daily": get_int_setting(conn, "backup_keep_daily", 14),
        "weekly": get_int_setting(conn, "backup_keep_weekly", 8),
        "monthly": get_int_setting(conn, "backup_keep_monthly", 24),
        "snapshots": SNAPSHOTS_KEPT,
    })
    if made:
        copy_to_external(conn, data, made[0], now)
    return made


def _copy_as(path: Path, data: DataDir, kind: str, now: datetime) -> Path:
    folder = data.backups / kind
    folder.mkdir(parents=True, exist_ok=True)
    target = _unique_path(folder, f"rental-{now:%Y%m%d-%H%M%S}-{kind}")
    shutil.copy2(path, target)
    return target


def copy_to_external(conn: sqlite3.Connection, data: DataDir, backup: Path,
                     now: datetime | None = None) -> bool:
    """Copy a backup and new documents to the external drive, if one is set and connected."""
    target = get_setting(conn, "backup_external_path").strip()
    if not target:
        return False
    root = Path(target).expanduser()
    if not root.is_dir():
        return False  # drive not plugged in; the dashboard warns when this goes on too long
    dest = root / "RentalTrackerBackups"
    (dest / "db").mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, dest / "db" / backup.name)
    sync_documents(data.documents, dest / "documents")
    for old in sorted((dest / "db").glob("rental-*.db"))[:-60]:
        old.unlink(missing_ok=True)
    set_setting(conn, "last_external_backup", (now or datetime.now()).isoformat(timespec="seconds"))
    return True


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def validate_backup(path: Path) -> int:
    """Return the backup's schema version, or raise ServiceError if it is unusable."""
    if not path.is_file():
        raise ServiceError("Backup file not found")
    try:
        src = sqlite3.connect(_readonly_uri(path), uri=True)
        try:
            ok = src.execute("PRAGMA integrity_check").fetchone()[0]
            version = src.execute("PRAGMA user_version").fetchone()[0]
            has_tables = src.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('leases','charges','payments')"
            ).fetchone()[0]
        finally:
            src.close()
    except sqlite3.DatabaseError as e:
        raise ServiceError(f"This is not a valid Rental Tracker backup ({e})") from None
    if ok != "ok" or has_tables != 3:
        raise ServiceError("The backup failed its integrity check and cannot be restored")
    if version > dbmod.latest_version():
        raise ServiceError("The backup was made by a newer version of the app")
    return version


def restore_backup(conn: sqlite3.Connection, data: DataDir, path: Path) -> Path:
    """Replace the live database with a backup. Returns the safety copy taken first."""
    validate_backup(path)
    safety = create_backup(conn, data, "snapshots", "pre-restore")
    src = sqlite3.connect(_readonly_uri(path), uri=True)
    try:
        src.backup(conn)
    finally:
        src.close()
    dbmod.migrate(conn)
    audit(conn, "restore", changes={"from": path.name, "safety_copy": safety.name})
    return safety


def delete_backup(data: DataDir, name: str) -> str:
    path = resolve_backup(data, name)
    if len(list_backups(data)) <= 1:
        raise ServiceError("This is your only backup, so it can't be deleted.")
    path.unlink()
    return path.name


def resolve_backup(data: DataDir, name: str) -> Path:
    """Find a backup by file name, refusing anything outside the backups folder."""
    for b in list_backups(data):
        if b.name == name:
            return b.path
    raise ServiceError("Backup not found")
