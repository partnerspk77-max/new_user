"""
Storage engine for Property Timelines and Commercial Opportunities.
Maintains persistent SQLite tables for property graphs and actionable pipeline leads.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import sqlite3

from src.opportunity.models import CommercialOpportunity, PropertyTimeline
from src.storage.database import Database


OPPORTUNITY_SCHEMA_SQL = """
-- 1. Property Timelines (Aggregated by Folio)
CREATE TABLE IF NOT EXISTS property_timelines (
    folio TEXT PRIMARY KEY,
    address TEXT,
    total_permits INTEGER NOT NULL,
    trades_json TEXT NOT NULL,
    roof_permits_count INTEGER NOT NULL,
    has_active_roof_permit INTEGER NOT NULL,
    last_roof_permit_date TEXT,
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

-- 2. Commercial Opportunities Table
CREATE TABLE IF NOT EXISTS commercial_opportunities (
    opportunity_id TEXT PRIMARY KEY,
    folio TEXT NOT NULL,
    address TEXT,
    opportunity_type TEXT NOT NULL,
    target_audience TEXT NOT NULL,
    priority_score REAL NOT NULL,
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
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opp_audience ON commercial_opportunities (target_audience);
CREATE INDEX IF NOT EXISTS idx_opp_type ON commercial_opportunities (opportunity_type);
CREATE INDEX IF NOT EXISTS idx_opp_priority ON commercial_opportunities (priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_opp_folio ON commercial_opportunities (folio);
CREATE INDEX IF NOT EXISTS idx_opp_freshness ON commercial_opportunities (freshness_tier);
"""


class OpportunityStorage:
    """Manages database persistence and queries for property graphs and opportunities."""

    def __init__(self, db: Database):
        self.db = db
        self.initialize_schema()

    def initialize_schema(self) -> None:
        conn = self.db.get_connection()
        with conn:
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
                t.roof_permits_count,
                1 if t.has_active_roof_permit else 0,
                t.last_roof_permit_date,
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
                    folio, address, total_permits, trades_json, roof_permits_count,
                    has_active_roof_permit, last_roof_permit_date, has_active_non_roof_permit,
                    non_roof_renovations_json, contractors_json, residential_commercial,
                    first_permit_date, latest_permit_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(folio) DO UPDATE SET
                    address = excluded.address,
                    total_permits = excluded.total_permits,
                    trades_json = excluded.trades_json,
                    roof_permits_count = excluded.roof_permits_count,
                    has_active_roof_permit = excluded.has_active_roof_permit,
                    last_roof_permit_date = excluded.last_roof_permit_date,
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

    def save_opportunities(self, opportunities: List[CommercialOpportunity]) -> int:
        conn = self.db.get_connection()
        records = [
            (
                o.opportunity_id,
                o.folio,
                o.address,
                o.opportunity_type,
                o.target_audience,
                o.priority_score,
                o.freshness_tier,
                o.age_hours,
                o.age_days,
                1 if o.contractor_present else 0,
                o.contractor_name,
                o.trigger_trade,
                o.trigger_event,
                o.recommended_action,
                o.residential_commercial,
                o.latitude,
                o.longitude,
                o.created_at,
            )
            for o in opportunities
        ]

        with conn:
            # Clear old and bulk replace
            conn.execute("DELETE FROM commercial_opportunities")
            conn.executemany(
                """
                INSERT INTO commercial_opportunities (
                    opportunity_id, folio, address, opportunity_type, target_audience,
                    priority_score, freshness_tier, age_hours, age_days,
                    contractor_present, contractor_name, trigger_trade, trigger_event,
                    recommended_action, residential_commercial, latitude, longitude, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        return len(records)

    def get_opportunities(
        self,
        audience: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        if audience:
            aud = audience.upper()
            if "ROOF" in aud:
                target = "ROOFING_CONTRACTOR"
            elif "SUPP" in aud or "DIST" in aud:
                target = "SUPPLIER_DISTRIBUTOR"
            else:
                target = aud

            cursor.execute(
                """
                SELECT * FROM commercial_opportunities
                WHERE target_audience = ?
                ORDER BY priority_score DESC, age_hours ASC
                LIMIT ?
                """,
                (target, limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM commercial_opportunities
                ORDER BY priority_score DESC, age_hours ASC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(r) for r in cursor.fetchall()]

    def get_metrics(self) -> Dict[str, Any]:
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM property_timelines")
        total_properties = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM commercial_opportunities")
        total_opportunities = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT target_audience, COUNT(*) as c
            FROM commercial_opportunities
            GROUP BY target_audience
            """
        )
        by_audience = {r["target_audience"]: r["c"] for r in cursor.fetchall()}

        cursor.execute(
            """
            SELECT opportunity_type, COUNT(*) as c
            FROM commercial_opportunities
            GROUP BY opportunity_type
            ORDER BY c DESC
            """
        )
        by_type = {r["opportunity_type"]: r["c"] for r in cursor.fetchall()}

        cursor.execute(
            """
            SELECT freshness_tier, COUNT(*) as c
            FROM commercial_opportunities
            GROUP BY freshness_tier
            ORDER BY c DESC
            """
        )
        by_freshness = {r["freshness_tier"]: r["c"] for r in cursor.fetchall()}

        return {
            "total_properties": total_properties,
            "total_opportunities": total_opportunities,
            "by_audience": by_audience,
            "by_type": by_type,
            "by_freshness": by_freshness,
        }

    def export_opportunities(
        self,
        csv_roofers: Path,
        csv_suppliers: Path,
        json_all: Path,
    ) -> Dict[str, int]:
        """Exports segmented opportunities for roofers and suppliers."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM commercial_opportunities ORDER BY priority_score DESC, age_hours ASC")
        all_rows = [dict(r) for r in cursor.fetchall()]

        roofers_rows = [r for r in all_rows if r["target_audience"] == "ROOFING_CONTRACTOR"]
        suppliers_rows = [r for r in all_rows if r["target_audience"] == "SUPPLIER_DISTRIBUTOR"]

        # Export JSON
        json_all.parent.mkdir(parents=True, exist_ok=True)
        with open(json_all, "w", encoding="utf-8") as f:
            json.dump(all_rows, f, indent=2)

        # Export Roofer CSV
        if roofers_rows:
            csv_roofers.parent.mkdir(parents=True, exist_ok=True)
            with open(csv_roofers, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=roofers_rows[0].keys())
                writer.writeheader()
                writer.writerows(roofers_rows)

        # Export Supplier CSV
        if suppliers_rows:
            csv_suppliers.parent.mkdir(parents=True, exist_ok=True)
            with open(csv_suppliers, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=suppliers_rows[0].keys())
                writer.writeheader()
                writer.writerows(suppliers_rows)

        return {
            "total": len(all_rows),
            "roofers": len(roofers_rows),
            "suppliers": len(suppliers_rows),
        }
