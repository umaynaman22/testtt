"""Attachments, stored on disk by content hash (BLUEPRINT §5)."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import sqlite3
from pathlib import Path

from ..config import DataDir
from .common import ServiceError, audit

RELATED_TYPES = ("owner", "property", "unit", "tenant", "lease", "vendor", "expense", "work_order",
                 "inspection", "inspection_item", "insurance_policy", "loan", "communication")
DOC_TYPES = ("lease", "addendum", "id", "screening", "receipt", "invoice", "photo", "insurance",
             "notice", "letter", "statement", "other")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


def store(conn: sqlite3.Connection, data: DataDir, *, related_type: str, related_id: int,
          filename: str, content: bytes, title: str | None = None, doc_type: str = "other",
          expires_on: str | None = None) -> int:
    if related_type not in RELATED_TYPES:
        raise ServiceError("Unknown record type for attachment")
    if doc_type not in DOC_TYPES:
        doc_type = "other"
    if not content:
        raise ServiceError("The file is empty")
    sha = hashlib.sha256(content).hexdigest()
    ext = Path(filename or "").suffix.lower()
    ext = ext if _EXT_RE.match(ext) else ""
    rel = f"{sha[:2]}/{sha}{ext}"
    target = data.documents / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
    mime = mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
    cur = conn.execute(
        """INSERT INTO documents(related_type, related_id, doc_type, title, stored_path,
                                 original_filename, mime_type, size_bytes, sha256, expires_on)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (related_type, related_id, doc_type, (title or filename or "Document").strip()[:200], rel,
         filename, mime, len(content), sha, expires_on or None))
    audit(conn, "insert", "document", cur.lastrowid, {"title": title or filename})
    return cur.lastrowid


def for_entity(conn: sqlite3.Connection, related_type: str, related_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE related_type = ? AND related_id = ? ORDER BY created_at DESC, id DESC",
        (related_type, related_id)).fetchall()


def get(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise ServiceError("Document not found")
    return row


def file_path(data: DataDir, doc: sqlite3.Row) -> Path:
    root = data.documents.resolve()
    path = (root / doc["stored_path"]).resolve()
    if root not in path.parents:
        raise ServiceError("Invalid document path")
    return path


def remove(conn: sqlite3.Connection, doc_id: int) -> None:
    """Remove the record. The file stays on disk (another record may share it)."""
    doc = get(conn, doc_id)
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    audit(conn, "delete", "document", doc_id, {"title": doc["title"]})


def expiring(conn: sqlite3.Connection, before: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE expires_on IS NOT NULL AND expires_on <= ? ORDER BY expires_on",
        (before,)).fetchall()
