"""
Storage engine for Roofing Classification results and AI caching.
Maintains the dedicated roofing_permits table and memoizes AI decisions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sqlite3

from src.roofing.models import RoofingClassification
from src.storage.database import Database


ROOFING_SCHEMA_SQL = """
-- 1. Dedicated Clean Roofing Permits Table
CREATE TABLE IF NOT EXISTS roofing_permits (
    permit_id TEXT PRIMARY KEY,
    global_id TEXT,
    source_object_id INTEGER NOT NULL,
    folio TEXT,
    permit_number TEXT,
    process_number TEXT,
    address TEXT,
    unit TEXT,
    permit_type TEXT,
    roofing_job_type TEXT NOT NULL,
    roofing_confidence REAL NOT NULL,
    classification_source TEXT NOT NULL,
    classification_reason TEXT,
    classification_model TEXT,
    classification_version TEXT,
    category_1 TEXT,
    description_1 TEXT,
    category_2 TEXT,
    description_2 TEXT,
    category_3 TEXT,
    description_3 TEXT,
    category_4 TEXT,
    description_4 TEXT,
    category_5 TEXT,
    description_5 TEXT,
    issued_at TEXT,
    status TEXT,
    residential_commercial TEXT,
    proposed_use TEXT,
    comment TEXT,
    contractor_number TEXT,
    contractor_name TEXT,
    latitude REAL,
    longitude REAL,
    classification_updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_roofing_job_type ON roofing_permits (roofing_job_type);
CREATE INDEX IF NOT EXISTS idx_roofing_status ON roofing_permits (status);
CREATE INDEX IF NOT EXISTS idx_roofing_contractor ON roofing_permits (contractor_name);
CREATE INDEX IF NOT EXISTS idx_roofing_folio ON roofing_permits (folio);
CREATE INDEX IF NOT EXISTS idx_roofing_issued_at ON roofing_permits (issued_at);

-- 2. AI Decision Cache Table (Prevents Repeated Model Calls)
CREATE TABLE IF NOT EXISTS roofing_ai_cache (
    cache_key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class RoofingStorage:
    """Manages persistence of roofing classifications and classification cache."""

    def __init__(self, db: Database):
        self.db = db
        self.initialize_schema()

    def initialize_schema(self) -> None:
        conn = self.db.get_connection()
        with conn:
            conn.executescript(ROOFING_SCHEMA_SQL)

    def get_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT result_json FROM roofing_ai_cache WHERE cache_key = ?", (cache_key,))
        row = cursor.fetchone()
        return json.loads(row["result_json"]) if row else None

    def set_cache(self, cache_key: str, payload: Dict[str, Any]) -> None:
        conn = self.db.get_connection()
        now_utc = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO roofing_ai_cache (cache_key, result_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET result_json = excluded.result_json
                """,
                (cache_key, json.dumps(payload), now_utc),
            )

    def get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        return self.get_cache(cache_key)

    def set(self, cache_key: str, payload: Dict[str, Any]) -> None:
        self.set_cache(cache_key, payload)

    def upsert_roofing_permit(
        self,
        permit: Dict[str, Any],
        classification: RoofingClassification,
    ) -> None:
        """Upserts a confirmed roofing permit into roofing_permits."""
        conn = self.db.get_connection()
        now_utc = datetime.now(timezone.utc).isoformat()

        with conn:
            conn.execute(
                """
                INSERT INTO roofing_permits (
                    permit_id, global_id, source_object_id, folio, permit_number, process_number,
                    address, unit, permit_type, roofing_job_type, roofing_confidence,
                    classification_source, classification_reason, classification_model, classification_version,
                    category_1, description_1, category_2, description_2, category_3, description_3,
                    category_4, description_4, category_5, description_5,
                    issued_at, status, residential_commercial, proposed_use, comment,
                    contractor_number, contractor_name, latitude, longitude,
                    classification_updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?
                )
                ON CONFLICT(permit_id) DO UPDATE SET
                    roofing_job_type = excluded.roofing_job_type,
                    roofing_confidence = excluded.roofing_confidence,
                    classification_source = excluded.classification_source,
                    classification_reason = excluded.classification_reason,
                    classification_model = excluded.classification_model,
                    classification_version = excluded.classification_version,
                    status = excluded.status,
                    contractor_name = excluded.contractor_name,
                    classification_updated_at = excluded.classification_updated_at
                """,
                (
                    permit.get("id"),
                    permit.get("global_id"),
                    permit.get("source_object_id"),
                    permit.get("folio"),
                    permit.get("permit_number"),
                    permit.get("process_number"),
                    permit.get("address"),
                    permit.get("unit"),
                    permit.get("permit_type"),
                    classification.job_type,
                    classification.confidence,
                    classification.classification_source,
                    classification.reason,
                    classification.classification_model,
                    classification.classification_version,
                    permit.get("category_1"),
                    permit.get("description_1"),
                    permit.get("category_2"),
                    permit.get("description_2"),
                    permit.get("category_3"),
                    permit.get("description_3"),
                    permit.get("category_4"),
                    permit.get("description_4"),
                    permit.get("category_5"),
                    permit.get("description_5"),
                    permit.get("issued_at"),
                    permit.get("status"),
                    permit.get("residential_commercial"),
                    permit.get("proposed_use"),
                    permit.get("comment"),
                    permit.get("contractor_number"),
                    permit.get("contractor_name"),
                    permit.get("latitude"),
                    permit.get("longitude"),
                    now_utc,
                ),
            )

    def count(self) -> int:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM roofing_permits")
        return cursor.fetchone()[0]

    def export_csv_and_json(self, csv_path: Path, json_path: Path) -> int:
        """Exports all confirmed roofing permits to CSV and JSON."""
        import csv
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM roofing_permits ORDER BY issued_at DESC, source_object_id DESC")
        rows = [dict(r) for r in cursor.fetchall()]

        if not rows:
            return 0

        # Export JSON
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)

        # Export CSV
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        return len(rows)
