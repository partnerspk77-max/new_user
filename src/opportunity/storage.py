"""
Storage engine for Property Timelines and Stateful Commercial Signals.
Maintains persistent SQLite tables for property graphs and signal histories.
Never drops historical signals on rebuilds: manages state transitions (ACTIVE -> RESOLVED).
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import sqlite3

from src.opportunity.models import PropertySignal, PropertyTimeline, SignalStatus
from src.storage.database import Database


OPPORTUNITY_SCHEMA_SQL = """
-- 1. Property Timelines (Aggregated by Folio)
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
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pt_active_roof ON property_timelines (has_active_roof_permit);
CREATE INDEX IF NOT EXISTS idx_pt_active_non_roof ON property_timelines (has_active_non_roof_permit);
CREATE INDEX IF NOT EXISTS idx_pt_roof_years ON property_timelines (years_since_last_roof_permit);

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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sig_folio ON property_signals (folio);
CREATE INDEX IF NOT EXISTS idx_sig_status ON property_signals (status);
CREATE INDEX IF NOT EXISTS idx_sig_type ON property_signals (signal_type);
CREATE INDEX IF NOT EXISTS idx_sig_audience ON property_signals (target_audience);
CREATE INDEX IF NOT EXISTS idx_sig_score ON property_signals (evidence_score DESC);

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
            # Check if property_timelines needs column migration
            cursor.execute("PRAGMA table_info(property_timelines)")
            existing_cols = {r["name"] for r in cursor.fetchall()}
            if existing_cols and "years_since_last_roof_permit" not in existing_cols:
                # Re-create property_timelines with new schema
                cursor.execute("DROP TABLE IF EXISTS property_timelines")

            # Check if commercial_opportunities is a table or view and drop cleanly
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
                    first_permit_date, latest_permit_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    latitude, longitude, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    updated_at = excluded.updated_at
                """,
                upsert_records,
            )

            # 2. Transition signals that were active but are no longer observed to RESOLVED
            # (e.g. project completed or reroof permit was finally pulled)
            disappeared_ids = set(existing_history.keys()) - incoming_ids
            if disappeared_ids:
                placeholders = ",".join("?" for _ in disappeared_ids)
                conn.execute(
                    f"""
                    UPDATE property_signals
                    SET status = '{SignalStatus.RESOLVED}', updated_at = ?
                    WHERE signal_id IN ({placeholders}) AND status = '{SignalStatus.ACTIVE}'
                    """,
                    [now_utc, *disappeared_ids],
                )

        return len(signals)

    def save_opportunities(self, opportunities: List[PropertySignal]) -> int:
        """Backward compatibility alias for save_signals."""
        return self.save_signals(opportunities)

    def get_signals(
        self,
        audience: Optional[str] = None,
        status: str = SignalStatus.ACTIVE,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        if audience:
            aud = audience.upper()
            target = "ROOFING_CONTRACTOR" if "ROOF" in aud else ("SUPPLIER_DISTRIBUTOR" if ("SUPP" in aud or "DIST" in aud) else aud)
            cursor.execute(
                """
                SELECT * FROM property_signals
                WHERE target_audience = ? AND status = ?
                ORDER BY evidence_score DESC, age_hours ASC
                LIMIT ?
                """,
                (target, status, limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM property_signals
                WHERE status = ?
                ORDER BY evidence_score DESC, age_hours ASC
                LIMIT ?
                """,
                (status, limit),
            )
        return [dict(r) for r in cursor.fetchall()]

    def get_opportunities(
        self,
        audience: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Backward compatibility alias returning active signals."""
        return self.get_signals(audience=audience, status=SignalStatus.ACTIVE, limit=limit)

    def get_metrics(self) -> Dict[str, Any]:
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
            ORDER BY c DESC
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
