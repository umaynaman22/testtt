"""SQLite connection handling and schema migrations.

Connections run in autocommit mode; use ``transaction()`` for anything that
writes more than one row so it either fully happens or not at all.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[\w-]+\.sql$")


class NewerDatabaseError(RuntimeError):
    """The database was written by a newer version of the app."""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception.

    Nested calls join the outer transaction.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrations() -> list[tuple[int, Path]]:
    found = []
    for path in MIGRATIONS_DIR.iterdir():
        m = _MIGRATION_RE.match(path.name)
        if m:
            found.append((int(m.group(1)), path))
    return sorted(found)


def latest_version() -> int:
    return migrations()[-1][0]


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, before: Callable[[], object] | None = None) -> int:
    """Apply pending migrations in order. Returns how many were applied.

    ``before`` runs once before the first migration on an existing database
    (used to take a backup). Each migration is atomic.
    """
    current = schema_version(conn)
    if current > latest_version():
        raise NewerDatabaseError(
            f"This database is schema version {current}, but this copy of the app only "
            f"understands up to version {latest_version()}. Install the newer app version."
        )
    pending = [(v, p) for v, p in migrations() if v > current]
    if not pending:
        return 0
    if before is not None and current > 0:
        before()
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return len(pending)
