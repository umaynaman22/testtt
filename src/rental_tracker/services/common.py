"""Shared helpers for services: errors, settings, audit log, books lock."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from ..domain.periods import parse_date


class ServiceError(ValueError):
    """A problem the user can fix (shown as a message, never a crash)."""


class LockedPeriodError(ServiceError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(get_setting(conn, key, str(default)) or default)
    except ValueError:
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def audit(conn: sqlite3.Connection, action: str, entity_type: str | None = None,
          entity_id: int | None = None, changes: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(action, entity_type, entity_id, changes_json) VALUES (?, ?, ?, ?)",
        (action, entity_type, entity_id,
         json.dumps(changes, default=str, sort_keys=True) if changes else None))


def books_locked_through(conn: sqlite3.Connection) -> date | None:
    value = get_setting(conn, "books_locked_through")
    return parse_date(value) if value else None


def ensure_open(conn: sqlite3.Connection, when: date | str) -> None:
    """Refuse to create or change financial entries dated inside the locked period."""
    lock = books_locked_through(conn)
    if lock and parse_date(when) <= lock:
        raise LockedPeriodError(
            f"The books are locked through {lock.isoformat()}. Post an adjustment dated after "
            "that instead, or change the lock date in Settings.")


def diff(old: sqlite3.Row | dict | None, new: dict[str, Any]) -> dict[str, list[Any]]:
    """{field: [old, new]} for fields that changed (for the audit log)."""
    old = dict(old) if old is not None else {}
    return {k: [old.get(k), v] for k, v in new.items() if old.get(k) != v}


def row_or_error(conn: sqlite3.Connection, sql: str, params: tuple, what: str) -> sqlite3.Row:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise ServiceError(f"{what} not found")
    return row
