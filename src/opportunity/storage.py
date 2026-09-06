"""
Storage repository for Miami-Dade Property Graph and Stateful Signals.
Implements stateful persistence (preserving first_detected_at, lifecycle states)
and provides historical tracking without destructive truncations.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger
from src.opportunity.models import (
    PropertySignal,
    PropertyTimeline,
    SignalStatus,
)
from src.storage.database import Database


OPPORTUNITY_SCHEMA_SQL = """
-- 1. Property Graph Timelines Table
CREATE TABLE IF NOT EXISTS property_timelines (
    folio TEXT PRIMARY KEY,
    address TEXT,
    total_permits INTEGER NOT NULL,
    trades_json TEXT NOT NULL,
    active_trades_json TEXT NOT NULL,
    roof_permits_count INTEGER NOT NULL,
    has_active_roof_permit INTEGER NOT NULL,
    last_roof_permit_date TEXT,
    years_since_last_roof_permit REAL,
    last_roof_system TEXT,
    last_roof_contractor TEXT,
    has_active_non_roof_permit INTEGER NOT NULL,
    non_roof_renovations_json TEXT NOT NULL,
    contractors_json TEXT NOT NULL,
    residential_commercial TEXT,
    first_permit_date TEXT,
    latest_permit_date TEXT,
    year_built INTEGER,
    building_actual_area REAL,
    building_heated_area REAL,
    dor_desc TEXT,
    owner_name TEXT,
    assessed_value REAL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pt_active_roof ON property_timelines (has_active_roof_permit);
CREATE INDEX IF NOT EXISTS idx_pt_active_non_roof ON property_timelines (has_active_non_roof_permit);
CREATE INDEX IF NOT EXISTS idx_pt_roof_years ON property_timelines (years_since_last_roof_permit);
CREATE INDEX IF NOT EXISTS idx_pt_year_built ON property_timelines (year_built);

-- 2. Stateful Property Signals Table (Preserves History Across Syncs)
CREATE TABLE IF NOT EXISTS property_signals (
    signal_id TEXT PRIMARY KEY,
    folio TEXT NOT NULL,
    address TEXT,
    signal_type TEXT NOT NULL,
    target_audience TEXT NOT NULL,
    status TEXT NOT NULL, -- 'ACTIVE', 'RESOLVED', 'EXPIRED'
    first_detected_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    evidence_score REAL NOT NULL,
    evidence_breakdown_json TEXT NOT NULL,
    corroborating_signals_json TEXT NOT NULL,
    unverified_assumptions_json TEXT NOT NULL,
    freshness_tier TEXT NOT NULL,
    age_hours REAL,
    age_days REAL,
    contractor_present INTEGER NOT NULL,
    contractor_name TEXT,
    trigger_trade TEXT NOT NULL,
    trigger_event TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    residential_commercial TEXT,
    latitude REAL,
    longitude REAL,
    year_built INTEGER,
    building_age INTEGER,
    building_actual_area REAL,
    estimated_roof_squares REAL,
    owner_name TEXT,
    dor_desc TEXT,
    assessed_value REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sig_folio ON property_signals (folio);
CREATE INDEX IF NOT EXISTS idx_sig_status ON property_signals (status);
CREATE INDEX IF NOT EXISTS idx_sig_type ON property_signals (signal_type);
CREATE INDEX IF NOT EXISTS idx_sig_audience ON property_signals (target_audience);
CREATE INDEX IF NOT EXISTS idx_sig_score ON property_signals (evidence_score DESC);
CREATE INDEX IF NOT EXISTS idx_sig_age ON property_signals (building_age);

-- Backward compatibility view for legacy commercial_opportunities queries
CREATE VIEW IF NOT EXISTS commercial_opportunities AS
SELECT
    signal_id AS opportunity_id,
    folio,
    address,
    signal_type AS opportunity_type,
    target_audience,
    evidence_score AS priority_score,
    freshness_tier,
    age_hours,
    age_days,
    contractor_present,
    contractor_name,
    trigger_trade,
    trigger_event,
    recommended_action,
    residential_commercial,
    latitude,
    longitude,
    year_built,
    building_age,
    building_actual_area,
    estimated_roof_squares,
    owner_name,
    dor_desc,
    assessed_value,
    created_at
FROM property_signals
WHERE status = 'ACTIVE';
"""


class OpportunityStorage:
    """Manages database persistence and queries for property graphs and stateful signals."""

    def __init__(self, db: Database):
        self.db = db
        self.initialize_schema()

    def initialize_schema(self) -> None:
        conn = self.db.get_connection()
        with conn:
            cursor = conn.cursor()

            # Ensure property_timelines columns
            cursor.execute("PRAGMA table_info(property_timelines)")
            pt_cols = {r["name"] for r in cursor.fetchall()}
            if pt_cols:
                timeline_additions = [
                    ("year_built", "INTEGER"),
                    ("building_actual_area", "REAL"),
                    ("building_heated_area", "REAL"),
                    ("dor_desc", "TEXT"),
                    ("owner_name", "TEXT"),
                    ("assessed_value", "REAL"),
                ]
                for col_name, col_type in timeline_additions:
                    if col_name not in pt_cols:
                        cursor.execute(f"ALTER TABLE property_timelines ADD COLUMN {col_name} {col_type}")

            # Ensure property_signals columns
            cursor.execute("PRAGMA table_info(property_signals)")
            sig_cols = {r["name"] for r in cursor.fetchall()}
            if sig_cols:
                signal_additions = [
                    ("year_built", "INTEGER"),
                    ("building_age", "INTEGER"),
                    ("building_actual_area", "REAL"),
                    ("estimated_roof_squares", "REAL"),
                    ("owner_name", "TEXT"),
                    ("dor_desc", "TEXT"),
                    ("assessed_value", "REAL"),
                ]
                for col_name, col_type in signal_additions:
                    if col_name not in sig_cols:
                        cursor.execute(f"ALTER TABLE property_signals ADD COLUMN {col_name} {col_type}")

            # Check if commercial_opportunities is a table or view and drop cleanly to recreate
            cursor.execute("SELECT type FROM sqlite_master WHERE name = 'commercial_opportunities'")
            row = cursor.fetchone()
            if row:
                if row["type"] == "view":
                    cursor.execute("DROP VIEW commercial_opportunities")
                elif row["type"] == "table":
                    cursor.execute("DROP TABLE commercial_opportunities")

            conn.executescript(OPPORTUNITY_SCHEMA_SQL)

    def save_timelines(self, timelines: Dict[str, PropertyTimeline]) -> int:
        conn = self.db.get_connection()
        now_utc = datetime.now(timezone.utc).isoformat()
        records = [
            (
                t.folio,
                t.address,
                t.total_permits,
                json.dumps(t.trades),
                json.dumps(t.active_trades),
                t.roof_permits_count,
                1 if t.has_active_roof_permit else 0,
                t.last_roof_permit_date,
                t.years_since_last_roof_permit,
                t.last_roof_system,
                t.last_roof_contractor,
                1 if t.has_active_non_roof_permit else 0,
                json.dumps(t.non_roof_renovation_types),
                json.dumps(t.contractors_seen),
                t.residential_commercial,
                t.first_permit_date,
                t.latest_permit_date,
                t.year_built,
                t.building_actual_area,
                t.building_heated_area,
                t.dor_desc,
                t.owner_name,
                t.assessed_value,
                now_utc,
            )
            for t in timelines.values()
        ]

        with conn:
            conn.executemany(
                """
                INSERT INTO property_timelines (
                    folio, address, total_permits, trades_json, active_trades_json, roof_permits_count,
                    has_active_roof_permit, last_roof_permit_date, years_since_last_roof_permit,
                    last_roof_system, last_roof_contractor, has_active_non_roof_permit,
                    non_roof_renovations_json, contractors_json, residential_commercial,
                    first_permit_date, latest_permit_date, year_built, building_actual_area,
                    building_heated_area, dor_desc, owner_name, assessed_value, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(folio) DO UPDATE SET
                    address = excluded.address,
                    total_permits = excluded.total_permits,
                    trades_json = excluded.trades_json,
                    active_trades_json = excluded.active_trades_json,
                    roof_permits_count = excluded.roof_permits_count,
                    has_active_roof_permit = excluded.has_active_roof_permit,
                    last_roof_permit_date = excluded.last_roof_permit_date,
                    years_since_last_roof_permit = excluded.years_since_last_roof_permit,
                    last_roof_system = excluded.last_roof_system,
                    last_roof_contractor = excluded.last_roof_contractor,
                    has_active_non_roof_permit = excluded.has_active_non_roof_permit,
                    non_roof_renovations_json = excluded.non_roof_renovations_json,
                    contractors_json = excluded.contractors_json,
                    residential_commercial = excluded.residential_commercial,
                    latest_permit_date = excluded.latest_permit_date,
                    year_built = excluded.year_built,
                    building_actual_area = excluded.building_actual_area,
                    building_heated_area = excluded.building_heated_area,
                    dor_desc = excluded.dor_desc,
                    owner_name = excluded.owner_name,
                    assessed_value = excluded.assessed_value,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def save_signals(self, signals: List[PropertySignal]) -> int:
        """
        Statefully persists commercial signals.
        Never drops signal history:
        - Inserts newly discovered signals (first_detected_at = now)
        - Updates active signals (last_seen_at = now, status = ACTIVE)
        - Transitions disappeared signals (status = RESOLVED)
        """
        conn = self.db.get_connection()
        now_utc = datetime.now(timezone.utc).isoformat()
        cursor = conn.cursor()

        # Fetch existing active signals to preserve first_detected_at
        cursor.execute("SELECT signal_id, first_detected_at FROM property_signals")
        existing_history = {r["signal_id"]: r["first_detected_at"] for r in cursor.fetchall()}

        incoming_ids = set()
        upsert_records = []

        for s in signals:
            incoming_ids.add(s.signal_id)
            first_seen = existing_history.get(s.signal_id, now_utc)

            upsert_records.append(
                (
                    s.signal_id,
                    s.folio,
                    s.address,
                    s.signal_type,
                    s.target_audience,
                    SignalStatus.ACTIVE,
                    first_seen,
                    now_utc,
                    s.evidence_score,
                    json.dumps(s.evidence_breakdown),
                    json.dumps(s.corroborating_signals),
                    json.dumps(s.unverified_assumptions),
                    s.freshness_tier,
                    s.age_hours,
                    s.age_days,
                    1 if s.contractor_present else 0,
                    s.contractor_name,
                    s.trigger_trade,
                    s.trigger_event,
                    s.recommended_action,
                    s.residential_commercial,
                    s.latitude,
                    s.longitude,
                    s.year_built,
                    s.building_age,
                    s.building_actual_area,
                    s.estimated_roof_squares,
                    s.owner_name,
                    s.dor_desc,
                    s.assessed_value,
                    first_seen,
                    now_utc,
                )
            )

        with conn:
            # 1. Upsert all active incoming signals
            conn.executemany(
                """
                INSERT INTO property_signals (
                    signal_id, folio, address, signal_type, target_audience,
                    status, first_detected_at, last_seen_at, evidence_score,
                    evidence_breakdown_json, corroborating_signals_json, unverified_assumptions_json,
                    freshness_tier, age_hours, age_days, contractor_present, contractor_name,
                    trigger_trade, trigger_event, recommended_action, residential_commercial,
                    latitude, longitude, year_built, building_age, building_actual_area,
                    estimated_roof_squares, owner_name, dor_desc, assessed_value,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    evidence_score = excluded.evidence_score,
                    evidence_breakdown_json = excluded.evidence_breakdown_json,
                    corroborating_signals_json = excluded.corroborating_signals_json,
                    unverified_assumptions_json = excluded.unverified_assumptions_json,
                    freshness_tier = excluded.freshness_tier,
                    age_hours = excluded.age_hours,
                    age_days = excluded.age_days,
                    contractor_present = excluded.contractor_present,
                    contractor_name = excluded.contractor_name,
                    trigger_event = excluded.trigger_event,
                    recommended_action = excluded.recommended_action,
                    year_built = excluded.year_built,
                    building_age = excluded.building_age,
                    building_actual_area = excluded.building_actual_area,
                    estimated_roof_squares = excluded.estimated_roof_squares,
                    owner_name = excluded.owner_name,
                    dor_desc = excluded.dor_desc,
                    assessed_value = excluded.assessed_value,
                    updated_at = excluded.updated_at
                """,
                upsert_records,
            )

            # 2. Transition signals that were active but are no longer observed to RESOLVED
            all_active_cursor = conn.cursor()
            all_active_cursor.execute("SELECT signal_id FROM property_signals WHERE status = 'ACTIVE'")
            current_active_ids = {r["signal_id"] for r in all_active_cursor.fetchall()}
            missing_ids = list(current_active_ids - incoming_ids)

            if missing_ids:
                placeholders = ",".join("?" for _ in missing_ids)
                conn.execute(
                    f"""
                    UPDATE property_signals
                    SET status = 'RESOLVED', updated_at = ?
                    WHERE signal_id IN ({placeholders})
                    """,
                    [now_utc] + missing_ids,
                )
                logger.info(f"Transitioned {len(missing_ids)} resolved signals to RESOLVED status.")

        logger.info(f"Statefully persisted {len(upsert_records)} signals into property_signals.")
        return len(upsert_records)

    def save_opportunities(self, opportunities: List[PropertySignal]) -> int:
        """Backward compatibility alias."""
        return self.save_signals(opportunities)

    def get_signals(
        self,
        audience: Optional[str] = None,
        signal_type: Optional[str] = None,
        status: str = SignalStatus.ACTIVE,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Retrieves ranked signals with deserialized evidence payloads."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM property_signals WHERE status = ?"
        params: List[Any] = [status]

        if audience:
            aud_upper = audience.upper()
            if "ROOF" in aud_upper:
                query += " AND target_audience = 'ROOFING_CONTRACTOR'"
            elif "SUPPLIER" in aud_upper or "DISTRIBUTOR" in aud_upper:
                query += " AND target_audience = 'SUPPLIER_DISTRIBUTOR'"
            else:
                query += " AND target_audience = ?"
                params.append(aud_upper)

        if signal_type:
            query += " AND signal_type = ?"
            params.append(signal_type.upper())

        query += " ORDER BY evidence_score DESC, age_hours ASC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = [dict(r) for r in cursor.fetchall()]

        for r in rows:
            for json_field in ("evidence_breakdown_json", "corroborating_signals_json", "unverified_assumptions_json"):
                if r.get(json_field):
                    try:
                        r[json_field.replace("_json", "")] = json.loads(r[json_field])
                    except Exception:
                        r[json_field.replace("_json", "")] = []

        return rows

    def get_opportunities(
        self,
        audience: Optional[str] = None,
        opportunity_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Backward compatibility alias."""
        return self.get_signals(audience=audience, signal_type=opportunity_type, limit=limit)

    def get_stats(self) -> Dict[str, Any]:
        """Returns statistical counts for property graph and signals."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM property_timelines")
        total_properties = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM property_signals WHERE status = 'ACTIVE'")
        active_signals = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM property_signals WHERE status = 'RESOLVED'")
        resolved_signals = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT target_audience, COUNT(*) as c
            FROM property_signals WHERE status = 'ACTIVE'
            GROUP BY target_audience
            """
        )
        by_audience = {r["target_audience"]: r["c"] for r in cursor.fetchall()}

        cursor.execute(
            """
            SELECT signal_type, COUNT(*) as c
            FROM property_signals WHERE status = 'ACTIVE'
            GROUP BY signal_type
            ORDER BY c DESC
            """
        )
        by_type = {r["signal_type"]: r["c"] for r in cursor.fetchall()}

        cursor.execute(
            """
            SELECT freshness_tier, COUNT(*) as c
            FROM property_signals WHERE status = 'ACTIVE'
            GROUP BY freshness_tier
            """
        )
        by_freshness = {r["freshness_tier"]: r["c"] for r in cursor.fetchall()}

        return {
            "total_properties": total_properties,
            "total_opportunities": active_signals,  # Alias
            "active_signals": active_signals,
            "resolved_signals": resolved_signals,
            "by_audience": by_audience,
            "by_type": by_type,
            "by_freshness": by_freshness,
        }

    def export_signals(
        self,
        csv_roofers: Path,
        csv_suppliers: Path,
        json_all: Path,
    ) -> Dict[str, int]:
        """Exports segmented signal feeds with parsed evidence payloads."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT * FROM property_signals
            WHERE status = 'ACTIVE'
            ORDER BY evidence_score DESC, age_hours ASC
            """
        )
        all_rows = [dict(r) for r in cursor.fetchall()]

        roofers_rows = [r for r in all_rows if r["target_audience"] == "ROOFING_CONTRACTOR"]
        suppliers_rows = [r for r in all_rows if r["target_audience"] == "SUPPLIER_DISTRIBUTOR"]

        # Export JSON
        json_all.parent.mkdir(parents=True, exist_ok=True)
        with open(json_all, "w", encoding="utf-8") as f:
            json.dump(all_rows, f, indent=2)

        # Helper for CSV export to keep evidence readable
        def write_csv(p: Path, rows: List[Dict[str, Any]]):
            if not rows:
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)

        write_csv(csv_roofers, roofers_rows)
        write_csv(csv_suppliers, suppliers_rows)

        return {
            "total": len(all_rows),
            "roofers": len(roofers_rows),
            "suppliers": len(suppliers_rows),
        }

    def export_opportunities(
        self,
        csv_roofers: Path,
        csv_suppliers: Path,
        json_all: Path,
    ) -> Dict[str, int]:
        """Backward compatibility alias."""
        return self.export_signals(csv_roofers, csv_suppliers, json_all)

    # Alias for get_stats
    get_metrics = get_stats
