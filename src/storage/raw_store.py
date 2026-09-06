"""
Raw record store.
Preserves the complete original API response/payload for auditability and replay.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from src.models.permit import RawRecord
from src.storage.database import Database


class RawStore:
    """Handles persistence and retrieval of unmodified raw API payloads."""

    def __init__(self, db: Database):
        self.db = db

    def insert_batch(self, records: List[RawRecord]) -> int:
        """Appends a batch of raw records to raw_permits table."""
        if not records:
            return 0

        conn = self.db.get_connection()
        rows = [r.to_db_row() for r in records]

        with conn:
            conn.executemany(
                """
                INSERT INTO raw_permits (
                    source, source_object_id, global_id, fetched_at, raw_payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(records)

    def get_by_object_id(self, source: str, source_object_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves most recent raw record for an object ID."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT source, source_object_id, global_id, fetched_at, raw_payload
            FROM raw_permits
            WHERE source = ? AND source_object_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (source, source_object_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "source": row["source"],
            "source_object_id": row["source_object_id"],
            "global_id": row["global_id"],
            "fetched_at": row["fetched_at"],
            "raw_payload": json.loads(row["raw_payload"]),
        }

    def count(self) -> int:
        """Returns total count of stored raw records."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM raw_permits")
        return cursor.fetchone()[0]
