"""
Storage repository for Miami-Dade Property Graph and Stateful Signals.
Implements stateful persistence (preserving first_detected_at, lifecycle states),
NOAA storm correlation fields, and epistemic verification tracking.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger
from src.opportunity.models import (
    PropertySignal,
    PropertyTimeline,
    SignalStatus,
)
from src.storage.database import Database

# Canonical column order for signal/feed CSV exports when there are no rows yet.
SIGNAL_EXPORT_COLUMNS = [
    "signal_id", "folio", "address", "signal_type", "target_audience", "status",
    "first_detected_at", "last_seen_at", "evidence_score", "evidence_breakdown",
    "corroborating_signals", "unverified_assumptions", "freshness_tier",
    "age_hours", "age_days", "contractor_present", "contractor_name",
    "trigger_trade", "trigger_event", "recommended_action",
    "residential_commercial", "latitude", "longitude", "year_built",
    "building_age", "year_built_status", "roof_history_status",
    "building_actual_area", "estimated_roof_squares", "owner_name", "dor_desc",
    "assessed_value", "storm_event_type", "storm_distance_miles",
    "storm_event_date", "storm_age_days", "storm_magnitude",
    "created_at", "updated_at",
]


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
    year_built_status TEXT NOT NULL DEFAULT 'UNRECORDED',
    roof_history_status TEXT NOT NULL DEFAULT 'NO_PERMIT_IN_DATASET_WINDOW',
    building_actual_area REAL,
    estimated_roof_squares REAL,
    owner_name TEXT,
    dor_desc TEXT,
    assessed_value REAL,
    storm_event_type TEXT,
    storm_distance_miles REAL,
    storm_event_date TEXT,
    storm_age_days REAL,
    storm_magnitude TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sig_folio ON property_signals (folio);
CREATE INDEX IF NOT EXISTS idx_sig_status ON property_signals (status);
CREATE INDEX IF NOT EXISTS idx_sig_type ON property_signals (signal_type);
CREATE INDEX IF NOT EXISTS idx_sig_audience ON property_signals (target_audience);
CREATE INDEX IF NOT EXISTS idx_sig_score ON property_signals (evidence_score DESC);
CREATE INDEX IF NOT EXISTS idx_sig_age ON property_signals (building_age);
CREATE INDEX IF NOT EXISTS idx_sig_storm ON property_signals (storm_distance_miles);

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
    year_built_status,
    roof_history_status,
    building_actual_area,
    estimated_roof_squares,
    owner_name,
    dor_desc,
    assessed_value,
    storm_event_type,
    storm_distance_miles,
    storm_event_date,
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

            # Check existing columns in property_timelines
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

            # Check existing columns in property_signals
            cursor.execute("PRAGMA table_info(property_signals)")
            sig_cols = {r["name"] for r in cursor.fetchall()}
            if sig_cols:
                signal_additions = [
                    ("year_built", "INTEGER"),
                    ("building_age", "INTEGER"),
                    ("year_built_status", "TEXT DEFAULT 'UNRECORDED'"),
                    ("roof_history_status", "TEXT DEFAULT 'NO_PERMIT_IN_DATASET_WINDOW'"),
                    ("building_actual_area", "REAL"),
                    ("estimated_roof_squares", "REAL"),
                    ("owner_name", "TEXT"),
                    ("dor_desc", "TEXT"),
                    ("assessed_value", "REAL"),
                    ("storm_event_type", "TEXT"),
                    ("storm_distance_miles", "REAL"),
                    ("storm_event_date", "TEXT"),
                    ("storm_age_days", "REAL"),
                    ("storm_magnitude", "TEXT"),
                ]
                for col_name, col_type in signal_additions:
                    if col_name not in sig_cols:
                        cursor.execute(f"ALTER TABLE property_signals ADD COLUMN {col_name} {col_type}")

            # Recreate view cleanly
            cursor.execute("SELECT type FROM sqlite_master WHERE name = 'commercial_opportunities'")
            row = cursor.fetchone()
            if row:
                if row["type"] == "view":
                    cursor.execute("DROP VIEW commercial_opportunities")
                elif row["type"] == "table":
                    cursor.execute("DROP TABLE commercial_opportunities")

            conn.executescript(OPPORTUNITY_SCHEMA_SQL)

        try:
            from src.roofing.storage import RoofingStorage
            RoofingStorage(self.db)
        except Exception:
            pass

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
                    s.year_built_status,
                    s.roof_history_status,
                    s.building_actual_area,
                    s.estimated_roof_squares,
                    s.owner_name,
                    s.dor_desc,
                    s.assessed_value,
                    s.storm_event_type,
                    s.storm_distance_miles,
                    s.storm_event_date,
                    s.storm_age_days,
                    s.storm_magnitude,
                    first_seen,
                    now_utc,
                )
            )

        with conn:
            conn.executemany(
                """
                INSERT INTO property_signals (
                    signal_id, folio, address, signal_type, target_audience,
                    status, first_detected_at, last_seen_at, evidence_score,
                    evidence_breakdown_json, corroborating_signals_json, unverified_assumptions_json,
                    freshness_tier, age_hours, age_days, contractor_present, contractor_name,
                    trigger_trade, trigger_event, recommended_action, residential_commercial,
                    latitude, longitude, year_built, building_age, year_built_status,
                    roof_history_status, building_actual_area, estimated_roof_squares,
                    owner_name, dor_desc, assessed_value, storm_event_type, storm_distance_miles,
                    storm_event_date, storm_age_days, storm_magnitude, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    year_built_status = excluded.year_built_status,
                    roof_history_status = excluded.roof_history_status,
                    building_actual_area = excluded.building_actual_area,
                    estimated_roof_squares = excluded.estimated_roof_squares,
                    owner_name = excluded.owner_name,
                    dor_desc = excluded.dor_desc,
                    assessed_value = excluded.assessed_value,
                    storm_event_type = excluded.storm_event_type,
                    storm_distance_miles = excluded.storm_distance_miles,
                    storm_event_date = excluded.storm_event_date,
                    storm_age_days = excluded.storm_age_days,
                    storm_magnitude = excluded.storm_magnitude,
                    updated_at = excluded.updated_at
                """,
                upsert_records,
            )

            # Transition signals that were active but are no longer observed to RESOLVED
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
        return self.save_signals(opportunities)

    def get_signals(
        self,
        audience: Optional[str] = None,
        signal_type: Optional[str] = None,
        status: str = SignalStatus.ACTIVE,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        conn = self.db.get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM property_signals WHERE status = ?"
        params: List[Any] = [status]

        if audience and str(audience).strip().lower() not in ("all", "any", "*"):
            aud_upper = str(audience).upper()
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
        return self.get_signals(audience=audience, signal_type=opportunity_type, limit=limit)

    def get_stats(self) -> Dict[str, Any]:
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
            "total_opportunities": active_signals,
            "active_signals": active_signals,
            "resolved_signals": resolved_signals,
            "by_audience": by_audience,
            "by_type": by_type,
            "by_freshness": by_freshness,
        }

    get_metrics = get_stats

    def export_signals(
        self,
        csv_roofers: Path,
        csv_suppliers: Path,
        json_all: Path,
    ) -> Dict[str, int]:
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

        # Helper for CSV export — always writes a header row so downstream
        # consumers (Excel, BI tools) never receive empty/missing feed files.
        def write_csv(p: Path, rows: List[Dict[str, Any]]):
            p.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = list(rows[0].keys()) if rows else SIGNAL_EXPORT_COLUMNS
            with open(p, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
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
        return self.export_signals(csv_roofers, csv_suppliers, json_all)

    def get_contractor_market_intelligence(self, min_permits: int = 3) -> List[Dict[str, Any]]:
        """
        Aggregates contractor permit velocity, 30d growth rates, and market share.
        Compares trailing 30 days vs prior 30 days.
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(issued_at) FROM permits WHERE issued_at IS NOT NULL")
        max_row = cursor.fetchone()
        if not max_row or not max_row[0]:
            return []

        max_dt = datetime.fromisoformat(max_row[0].replace("Z", "+00:00"))
        t30_start = (max_dt - timedelta(days=30)).isoformat()
        p30_start = (max_dt - timedelta(days=60)).isoformat()

        query = """
        WITH recent AS (
            SELECT contractor_name, COUNT(*) as cnt_30d
            FROM permits
            WHERE contractor_name IS NOT NULL AND contractor_name != ''
              AND issued_at >= ?
            GROUP BY contractor_name
        ),
        prior AS (
            SELECT contractor_name, COUNT(*) as cnt_prior_30d
            FROM permits
            WHERE contractor_name IS NOT NULL AND contractor_name != ''
              AND issued_at >= ? AND issued_at < ?
            GROUP BY contractor_name
        ),
        roofing AS (
            SELECT contractor_name, COUNT(*) as roofing_cnt
            FROM roofing_permits
            WHERE contractor_name IS NOT NULL AND contractor_name != ''
            GROUP BY contractor_name
        ),
        total AS (
            SELECT contractor_name, COUNT(*) as total_permits, MAX(issued_at) as last_issued
            FROM permits
            WHERE contractor_name IS NOT NULL AND contractor_name != ''
            GROUP BY contractor_name
        )
        SELECT
            t.contractor_name,
            t.total_permits,
            COALESCE(r.cnt_30d, 0) as recent_30d_permits,
            COALESCE(p.cnt_prior_30d, 0) as prior_30d_permits,
            COALESCE(rf.roofing_cnt, 0) as roofing_permits_count,
            t.last_issued
        FROM total t
        LEFT JOIN recent r ON t.contractor_name = r.contractor_name
        LEFT JOIN prior p ON t.contractor_name = p.contractor_name
        LEFT JOIN roofing rf ON t.contractor_name = rf.contractor_name
        WHERE t.total_permits >= ?
        ORDER BY recent_30d_permits DESC, t.total_permits DESC
        """

        cursor.execute(query, (t30_start, p30_start, t30_start, min_permits))
        rows = cursor.fetchall()

        results = []
        for rank, r in enumerate(rows, start=1):
            recent_30d = r["recent_30d_permits"]
            prior_30d = r["prior_30d_permits"]
            if prior_30d > 0:
                growth_pct = round(((recent_30d - prior_30d) / prior_30d) * 100.0, 1)
            elif recent_30d > 0:
                growth_pct = 100.0
            else:
                growth_pct = 0.0

            results.append({
                "market_share_rank": rank,
                "contractor_name": r["contractor_name"],
                "total_permits": r["total_permits"],
                "recent_30d_permits": recent_30d,
                "prior_30d_permits": prior_30d,
                "velocity_growth_pct": growth_pct,
                "roofing_permits_count": r["roofing_permits_count"],
                "last_permit_issued": r["last_issued"],
            })

        return results

    def export_commercial_feeds(
        self,
        output_dir: Path = Path("data"),
        min_permits: int = 1,
    ) -> Dict[str, Any]:
        """
        Exports the 3 distinct commercial feeds:
        Feed 1: NEW_PERMITTED_PROJECT (for suppliers / distributors)
        Feed 2: PRE_PERMIT_ROOF_OPPORTUNITY (for roofing contractors)
        Feed 3: MARKET_INTELLIGENCE (for suppliers, manufacturers, large roofers)
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        f1_csv = output_dir / "feed_permitted_projects_suppliers.csv"
        f1_json = output_dir / "feed_permitted_projects_suppliers.json"

        f2_csv = output_dir / "feed_pre_permit_roof_opportunities.csv"
        f2_json = output_dir / "feed_pre_permit_roof_opportunities.json"

        f3_csv = output_dir / "feed_contractor_market_intelligence.csv"
        f3_json = output_dir / "feed_contractor_market_intelligence.json"

        self.export_signals(
            csv_roofers=f2_csv,
            csv_suppliers=f1_csv,
            json_all=output_dir / "property_signals.json",
        )

        suppliers_data = self.get_signals(audience="supplier", limit=5000)
        with open(f1_json, "w", encoding="utf-8") as f:
            json.dump(suppliers_data, f, indent=2)

        roofers_data = self.get_signals(audience="roofer", limit=5000)
        with open(f2_json, "w", encoding="utf-8") as f:
            json.dump(roofers_data, f, indent=2)

        intel_data = self.get_contractor_market_intelligence(min_permits=min_permits)
        with open(f3_json, "w", encoding="utf-8") as f:
            json.dump(intel_data, f, indent=2)

        if intel_data:
            with open(f3_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=intel_data[0].keys())
                writer.writeheader()
                writer.writerows(intel_data)

        # Backward compatibility aliases
        self.export_signals(
            output_dir / "property_signals_roofers.csv",
            output_dir / "property_signals_suppliers.csv",
            output_dir / "property_signals.json",
        )

        return {
            "feed_1_permitted_projects": len(suppliers_data),
            "feed_2_pre_permit_opportunities": len(roofers_data),
            "feed_3_market_intelligence_contractors": len(intel_data),
            "files": {
                "feed_1_csv": str(f1_csv),
                "feed_1_json": str(f1_json),
                "feed_2_csv": str(f2_csv),
                "feed_2_json": str(f2_json),
                "feed_3_csv": str(f3_csv),
                "feed_3_json": str(f3_json),
            },
        }
