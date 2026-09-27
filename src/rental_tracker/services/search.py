"""Global search over properties, units and tenants (SQLite FTS5)."""
from __future__ import annotations

import re
import sqlite3


def _join(*cols: str) -> str:
    """Space-join nullable columns (portable; concat_ws needs SQLite 3.44+)."""
    return " || ' ' || ".join(f"COALESCE({c}, '')" for c in cols)


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def rebuild(conn: sqlite3.Connection) -> None:
    """Rebuild the whole index. Fast enough (milliseconds) at portfolio scale to run after edits."""
    conn.execute("DELETE FROM search_index")
    conn.execute("""
        INSERT INTO search_index(entity_type, entity_id, title, body)
        SELECT 'property', p.id, p.name,
               """ + _join("p.address_line1", "p.address_line2", "p.city", "p.state", "p.postal_code",
                         "p.parcel_number", "p.notes",
                         "(SELECT group_concat(t.name, ' ') FROM property_tags pt "
                         "JOIN tags t ON t.id = pt.tag_id WHERE pt.property_id = p.id)") + """
          FROM properties p""")
    conn.execute("""
        INSERT INTO search_index(entity_type, entity_id, title, body)
        SELECT 'unit', u.id, p.code || ' · ' || u.unit_label,
               """ + _join("p.name", "p.address_line1", "u.notes") + """
          FROM units u JOIN properties p ON p.id = u.property_id
         WHERE (SELECT COUNT(*) FROM units x WHERE x.property_id = u.property_id) > 1""")
    conn.execute("""
        INSERT INTO search_index(entity_type, entity_id, title, body)
        SELECT 'tenant', id, first_name || ' ' || last_name,
               """ + _join("email", "phone", "alt_phone", "external_ref", "notes") + """
          FROM tenants""")


def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[sqlite3.Row]:
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return []
    match = " ".join(f'"{t}"*' for t in tokens[:10])
    return conn.execute(
        """SELECT entity_type, entity_id, title,
                  snippet(search_index, 3, '[', ']', '…', 12) AS snippet
             FROM search_index WHERE search_index MATCH ?
            ORDER BY rank LIMIT ?""", (match, limit)).fetchall()
