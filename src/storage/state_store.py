"""
Pipeline state management for checkpoints and restartability.
Enables interrupted jobs to safely resume without re-downloading or duplicate insertion.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.storage.database import Database


class StateStore:
    """Manages persistent synchronization checkpoints."""

    def __init__(self, db: Database):
        self.db = db

    def get_state(self, job_id: str = "miami_dade_arcgis") -> Optional[Dict[str, Any]]:
        """Retrieves last saved sync checkpoint."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, last_processed_date, last_source_object_id, last_run_at, total_ingested, status, metadata_json
            FROM sync_state WHERE id = ?
            """,
            (job_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "last_processed_date": row["last_processed_date"],
            "last_source_object_id": row["last_source_object_id"],
            "last_run_at": row["last_run_at"],
            "total_ingested": row["total_ingested"],
            "status": row["status"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        }

    def update_checkpoint(
        self,
        job_id: str = "miami_dade_arcgis",
        last_processed_date: Optional[str] = None,
        last_source_object_id: Optional[int] = None,
        total_ingested: Optional[int] = None,
        status: str = "completed",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Upserts current checkpoint state.

        Fields passed as ``None`` preserve their previous stored value, so a
        failure record never wipes out the running total of ingested records.
        """
        conn = self.db.get_connection()
        now_utc = datetime.now(timezone.utc).isoformat()
        metadata_str = json.dumps(metadata or {})

        with conn:
            conn.execute(
                """
                INSERT INTO sync_state (
                    id, last_processed_date, last_source_object_id, last_run_at, total_ingested, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_processed_date = COALESCE(excluded.last_processed_date, sync_state.last_processed_date),
                    last_source_object_id = COALESCE(excluded.last_source_object_id, sync_state.last_source_object_id),
                    last_run_at = excluded.last_run_at,
                    total_ingested = COALESCE(excluded.total_ingested, sync_state.total_ingested),
                    status = excluded.status,
                    metadata_json = excluded.metadata_json
                """,
                (job_id, last_processed_date, last_source_object_id, now_utc, total_ingested, status, metadata_str),
            )
