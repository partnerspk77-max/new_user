"""
Normalized permit store with deterministic deduplication and upserts.
Preserves updates to existing permits while preventing duplicate records.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.models.permit import NormalizedPermit
from src.storage.database import Database


class PermitStore:
    """Handles deterministic persistence, deduplication, and updates for normalized permits."""

    def __init__(self, db: Database):
        self.db = db

    def upsert_batch(self, permits: List[NormalizedPermit]) -> Tuple[int, int, int]:
        """
        Deterministically upserts a batch of normalized permits.
        Returns:
            Tuple of (new_count, updated_count, duplicate_count)
        """
        if not permits:
            return 0, 0, 0

        conn = self.db.get_connection()
        now_utc = datetime.now(timezone.utc).isoformat()

        # Gather existing records to classify new vs updated vs duplicate.
        # Chunked in pairs of <=400 to stay under SQLITE_MAX_VARIABLE_NUMBER=999
        # on older SQLite builds (2 params per permit x page_size=1000 = 2000 vars).
        source_pairs = [(p.source, p.source_object_id) for p in permits]
        existing_rows: Dict[Tuple[str, int], Dict[str, Any]] = {}
        cursor = conn.cursor()
        CHUNK_SIZE = 400
        for i in range(0, len(source_pairs), CHUNK_SIZE):
            chunk = source_pairs[i : i + CHUNK_SIZE]
            placeholders = ",".join("(?, ?)" for _ in chunk)
            flat_params: List[Any] = []
            for s, oid in chunk:
                flat_params.extend([s, oid])
            cursor.execute(
                f"""
                SELECT source, source_object_id, status, contractor_name, contractor_number,
                       last_inspection_at, completion_at, last_approval_at, renewal_at, comment
                FROM permits
                WHERE (source, source_object_id) IN (VALUES {placeholders})
                """,
                flat_params,
            )
            for r in cursor.fetchall():
                existing_rows[(r["source"], r["source_object_id"])] = dict(r)

        new_count = 0
        updated_count = 0
        duplicate_count = 0

        rows_to_insert = []

        for p in permits:
            key = (p.source, p.source_object_id)
            existing = existing_rows.get(key)

            if existing is None:
                new_count += 1
                record_updated_at = p.updated_at
            else:
                # Compare mutable fields to detect real updates
                has_changed = (
                    existing.get("status") != p.status
                    or existing.get("contractor_name") != p.contractor_name
                    or existing.get("contractor_number") != p.contractor_number
                    or existing.get("last_inspection_at") != p.last_inspection_at
                    or existing.get("completion_at") != p.completion_at
                    or existing.get("last_approval_at") != p.last_approval_at
                    or existing.get("renewal_at") != p.renewal_at
                    or existing.get("comment") != p.comment
                )
                if has_changed:
                    updated_count += 1
                    record_updated_at = now_utc
                else:
                    duplicate_count += 1
                    record_updated_at = existing.get("updated_at") or now_utc

            rows_to_insert.append((
                p.id,
                p.source,
                p.source_object_id,
                p.global_id,
                p.folio,
                p.permit_number,
                p.process_number,
                p.address,
                p.unit,
                p.is_condo,
                p.permit_type,
                p.category_1,
                p.description_1,
                p.category_2,
                p.description_2,
                p.category_3,
                p.description_3,
                p.category_4,
                p.description_4,
                p.category_5,
                p.description_5,
                p.category_6,
                p.description_6,
                p.category_7,
                p.description_7,
                p.category_8,
                p.description_8,
                p.category_9,
                p.description_9,
                p.category_10,
                p.description_10,
                p.issued_at,
                p.last_inspection_at,
                p.renewal_at,
                p.completion_at,
                p.last_approval_at,
                p.residential_commercial,
                p.proposed_use,
                p.application_type,
                p.comment,
                p.master_permit_number,
                p.contractor_number,
                p.contractor_name,
                p.status,
                p.latitude,
                p.longitude,
                p.source_fetched_at,
                p.created_at,
                record_updated_at,
            ))

        upsert_sql = """
        INSERT INTO permits (
            id, source, source_object_id, global_id, folio, permit_number, process_number,
            address, unit, is_condo, permit_type,
            category_1, description_1, category_2, description_2, category_3, description_3,
            category_4, description_4, category_5, description_5, category_6, description_6,
            category_7, description_7, category_8, description_8, category_9, description_9,
            category_10, description_10,
            issued_at, last_inspection_at, renewal_at, completion_at, last_approval_at,
            residential_commercial, proposed_use, application_type, comment,
            master_permit_number, contractor_number, contractor_name, status,
            latitude, longitude, source_fetched_at, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(source, source_object_id) DO UPDATE SET
            global_id = excluded.global_id,
            folio = excluded.folio,
            permit_number = excluded.permit_number,
            process_number = excluded.process_number,
            address = excluded.address,
            unit = excluded.unit,
            is_condo = excluded.is_condo,
            permit_type = excluded.permit_type,
            category_1 = excluded.category_1,
            description_1 = excluded.description_1,
            category_2 = excluded.category_2,
            description_2 = excluded.description_2,
            category_3 = excluded.category_3,
            description_3 = excluded.description_3,
            category_4 = excluded.category_4,
            description_4 = excluded.description_4,
            category_5 = excluded.category_5,
            description_5 = excluded.description_5,
            category_6 = excluded.category_6,
            description_6 = excluded.description_6,
            category_7 = excluded.category_7,
            description_7 = excluded.description_7,
            category_8 = excluded.category_8,
            description_8 = excluded.description_8,
            category_9 = excluded.category_9,
            description_9 = excluded.description_9,
            category_10 = excluded.category_10,
            description_10 = excluded.description_10,
            issued_at = excluded.issued_at,
            last_inspection_at = excluded.last_inspection_at,
            renewal_at = excluded.renewal_at,
            completion_at = excluded.completion_at,
            last_approval_at = excluded.last_approval_at,
            residential_commercial = excluded.residential_commercial,
            proposed_use = excluded.proposed_use,
            application_type = excluded.application_type,
            comment = excluded.comment,
            master_permit_number = excluded.master_permit_number,
            contractor_number = excluded.contractor_number,
            contractor_name = excluded.contractor_name,
            status = excluded.status,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            source_fetched_at = excluded.source_fetched_at,
            updated_at = excluded.updated_at
        """

        with conn:
            conn.executemany(upsert_sql, rows_to_insert)

        return new_count, updated_count, duplicate_count

    def get_by_id(self, permit_id: str) -> Optional[Dict[str, Any]]:
        """Finds permit by primary key."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM permits WHERE id = ?", (permit_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_by_object_id(self, source: str, source_object_id: int) -> Optional[Dict[str, Any]]:
        """Finds permit by source and source_object_id."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM permits WHERE source = ? AND source_object_id = ?",
            (source, source_object_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        """Returns total normalized permits in database."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM permits")
        return cursor.fetchone()[0]

    def get_latest_issued_date(self, source: str = "miami_dade_arcgis") -> Optional[str]:
        """Returns latest non-null issued_at timestamp in UTC ISO format."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT MAX(issued_at) FROM permits
            WHERE source = ? AND issued_at IS NOT NULL
            """,
            (source,),
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] else None

    def export_to_json(self, output_path: Path) -> int:
        """Exports all normalized permits to a JSON file."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM permits ORDER BY issued_at DESC, source_object_id DESC")
        rows = [dict(r) for r in cursor.fetchall()]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)

        return len(rows)
