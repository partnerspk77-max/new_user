"""
Commercial Viability and Precision Audit Engine for Miami-Dade Roofing Permits.
Extracts a stratified 200-record sample across all trade categories and job types,
performing a deep-dive evaluation of:
1. TRUE_ROOFING? (Technical scope accuracy)
2. JOB_TYPE_CORRECT? (Classification precision)
3. CONTRACTOR_ATTACHED? (Competitor presence)
4. OWNER_BUILDER? (Direct unrepresented prospect)
5. FRESHNESS_TIER (NEW, FRESH, RECENT, STALE, HISTORICAL)
6. ACTUAL_SALES_OPPORTUNITY? (Commercial actionability for another roofer)
7. DUPLICATE/PROJECT GROUPING (Clustered filings)
"""

from __future__ import annotations

import csv
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
import sqlite3

from src.storage.database import Database


class CommercialAuditEngine:
    """Executes stratified 200-record audit and computes commercial lead conversion metrics."""

    REFERENCE_DATE = "2026-09-03T00:00:00Z"

    def __init__(self, db: Database):
        self.db = db
        self.ref_dt = datetime.fromisoformat(self.REFERENCE_DATE.replace("Z", "+00:00"))

    def _calculate_freshness(self, issued_at: Optional[str]) -> Tuple[Optional[int], str]:
        if not issued_at:
            return None, "UNKNOWN"
        try:
            dt = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
            diff_days = max(0, (self.ref_dt - dt).days)
            if diff_days <= 1:
                return diff_days, "NEW (0-24h)"
            elif diff_days <= 3:
                return diff_days, "FRESH (1-3d)"
            elif diff_days <= 7:
                return diff_days, "RECENT (4-7d)"
            elif diff_days <= 30:
                return diff_days, "STALE (8-30d)"
            else:
                return diff_days, "HISTORICAL (30d+)"
        except Exception:
            return None, "UNKNOWN"

    def _check_related_project(self, folio: Optional[str], permit_number: str) -> Optional[str]:
        if not folio or not folio.strip():
            return None
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT permit_number, comment, issued_at FROM roofing_permits
            WHERE folio = ? AND permit_number != ?
            LIMIT 3
            """,
            (folio, permit_number),
        )
        rows = cursor.fetchall()
        if rows:
            related = [f"{r['permit_number']} ({r['comment'][:20] if r['comment'] else 'N/A'})" for r in rows]
            return f"Part of multi-permit folio ({len(rows)+1} permits total): " + ", ".join(related)
        return None

    def run_200_record_audit(self, random_seed: int = 42) -> List[Dict[str, Any]]:
        """Extracts and evaluates 200 stratified records."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        strata_queries = [
            ("CAT 0092 (GRAVEL/SBS/SINGLE PLY)", "category_1 = '0092'", 22),
            ("CAT 0095 (ASPHALT SHINGLE)", "category_1 = '0095'", 22),
            ("CAT 0096 (METAL/WOOD SHAKES)", "category_1 = '0096'", 22),
            ("CAT 0107 (TILE ROOF)", "category_1 = '0107'", 22),
            ("CAT 0109/0050/0106 (SECONDARY/CURB/WATERPROOF)", "category_1 IN ('0109', '0050', '0106') OR category_2 IN ('0109', '0050', '0106')", 20),
            ("JOB: SOLAR_ROOF_RELATED", "roofing_job_type = 'SOLAR_ROOF_RELATED'", 20),
            ("JOB: COMMERCIAL_ROOF", "roofing_job_type = 'COMMERCIAL_ROOF'", 20),
            ("JOB: REROOF", "roofing_job_type = 'REROOF'", 20),
            ("JOB: ROOF_REPAIR", "roofing_job_type = 'ROOF_REPAIR'", 4),  # All 4 in dataset
            ("JOB: ROOF_REPLACEMENT", "roofing_job_type = 'ROOF_REPLACEMENT'", 15),
            ("JOB: OTHER_ROOFING", "roofing_job_type = 'OTHER_ROOFING'", 15),
        ]

        rnd = random.Random(random_seed)
        sampled_permit_ids = set()
        audited_records: List[Dict[str, Any]] = []

        for stratum_name, condition, sample_size in strata_queries:
            cursor.execute(f"SELECT * FROM roofing_permits WHERE {condition}")
            available = [dict(r) for r in cursor.fetchall() if r["permit_id"] not in sampled_permit_ids]
            
            # Select random sample
            count_to_pick = min(sample_size, len(available))
            picked = rnd.sample(available, count_to_pick) if available else []

            for p in picked:
                sampled_permit_ids.add(p["permit_id"])
                evaluated = self._audit_single_record(p, stratum_name)
                audited_records.append(evaluated)

        # If slightly under 200 due to overlaps, top up from general pool
        if len(audited_records) < 200:
            deficit = 200 - len(audited_records)
            cursor.execute("SELECT * FROM roofing_permits")
            remaining = [dict(r) for r in cursor.fetchall() if r["permit_id"] not in sampled_permit_ids]
            top_up = rnd.sample(remaining, min(deficit, len(remaining)))
            for p in top_up:
                sampled_permit_ids.add(p["permit_id"])
                evaluated = self._audit_single_record(p, "GENERAL_TOP_UP")
                audited_records.append(evaluated)

        return audited_records

    def _audit_single_record(self, p: Dict[str, Any], stratum_name: str) -> Dict[str, Any]:
        """Deep-dive manual/expert analysis on a single permit record."""
        comment = str(p.get("comment") or "").upper()
        cat1 = str(p.get("category_1") or "")
        desc1 = str(p.get("description_1") or "")
        contractor = str(p.get("contractor_name") or "").strip()
        status = p.get("status")
        current_job = p.get("roofing_job_type")
        issued_at = p.get("issued_at")

        age_days, freshness_tier = self._calculate_freshness(issued_at)
        related_project = self._check_related_project(p.get("folio"), p.get("permit_number"))

        # 1. Determine True Roofing
        # True roofing means building envelope roof covering installation, reroofing, or repair.
        # False positives / cross-trade:
        is_solar = cat1 == "0034" or "SOLAR" in comment or current_job == "SOLAR_ROOF_RELATED"
        is_curb = cat1 == "0050" or "RAISE EXISTING ROOF" in comment or "ROOF MOUNT" in comment and cat1 in ["0003", "0050"]
        is_aluminum_patio = "ALUM" in comment and ("TERR" in comment or "AWN" in comment or cat1 == "0029")
        is_temp_power = "TEMP" in comment and "POW" in comment

        if is_temp_power:
            true_roofing = False
            corrected_job = "NOT_ROOFING"
            true_roofing_reason = "False positive keyword match; temporary electrical power service."
        elif is_curb:
            true_roofing = False
            corrected_job = "HVAC_ROOF_EQUIPMENT_RAISE"
            true_roofing_reason = "Mechanical HVAC trade raising curb equipment; not roofing trade work."
        elif is_aluminum_patio:
            true_roofing = False
            corrected_job = "ALUMINUM_PATIO_AWNING"
            true_roofing_reason = "Non-structural patio canopy/terrace cover; specialty aluminum awning trade."
        elif is_solar:
            true_roofing = False
            corrected_job = "SOLAR_ROOFTOP_PV"
            true_roofing_reason = "Electrical solar panel installation mounted on roof deck."
        else:
            true_roofing = True
            # Check corrected classification
            if any(w in comment for w in ["NEW SFR", "NEW HOME", "NEW RESIDENCE"]):
                corrected_job = "NEW_CONSTRUCTION_ROOF"
            elif cat1 == "0092" or "COMMERCIAL" in str(p.get("residential_commercial")).upper():
                corrected_job = "COMMERCIAL_ROOF"
            elif any(w in comment for w in ["REPAIR", "PATCH", "LEAK", "FASCIA"]):
                corrected_job = "ROOF_REPAIR"
            elif any(w in comment for w in ["REROOF", "RE-ROOF"]):
                corrected_job = "REROOF"
            else:
                corrected_job = "ROOF_REPLACEMENT"
            true_roofing_reason = f"Legitimate roof system work under Category {cat1} ({desc1})."

        # 2. Contractor & Owner-Builder status
        is_owner_builder = (
            not contractor
            or "OWNER" in contractor.upper()
            or "UNASSIGNED" in contractor.upper()
            or contractor.upper() in ["NONE", "NULL"]
        )
        contractor_attached = not is_owner_builder

        # 3. Actual Commercial Sales Opportunity for ANOTHER Roofer?
        # A licensed roofer looking for leads cannot sell to a job already won and under contract.
        if not true_roofing:
            sales_opportunity = False
            opportunity_type = "NON_ROOFING_TRADE"
            opportunity_reason = "Scope is solar, HVAC equipment, or non-roofing specialty trade."
        elif status == "F":
            sales_opportunity = False
            opportunity_type = "COMPLETED_HISTORICAL"
            opportunity_reason = "Permit is Finalized (passed final inspection). Job is already 100% completed."
        elif contractor_attached:
            # A licensed roofing contractor is already on the permit
            sales_opportunity = False
            opportunity_type = "COMPETITOR_ALREADY_WON"
            opportunity_reason = f"Job already contracted & permitted by licensed roofer: '{contractor}'."
        else:
            # Owner-builder or unassigned contractor
            if freshness_tier in ["NEW (0-24h)", "FRESH (1-3d)", "RECENT (4-7d)"]:
                sales_opportunity = True
                opportunity_type = "HIGH_VALUE_OWNER_BUILDER"
                opportunity_reason = "Fresh active permit without licensed roofer; homeowner pulled permit and may need contractor."
            else:
                sales_opportunity = False
                opportunity_type = "STALE_OWNER_BUILDER"
                opportunity_reason = f"Owner-builder permit is {age_days} days old ({freshness_tier}); likely work already in progress."

        return {
            "permit_id": p.get("permit_id"),
            "stratum": stratum_name,
            "permit_number": p.get("permit_number"),
            "process_number": p.get("process_number"),
            "address": p.get("address"),
            "folio": p.get("folio"),
            "permit_type": p.get("permit_type"),
            "category_1": cat1,
            "description_1": desc1,
            "category_2": p.get("category_2"),
            "description_2": p.get("description_2"),
            "comment": comment,
            "contractor_name": contractor or "(OWNER-BUILDER / UNASSIGNED)",
            "residential_commercial": p.get("residential_commercial"),
            "status": status,
            "issued_at": issued_at,
            "age_days": age_days,
            "freshness_tier": freshness_tier,
            "current_classification": current_job,
            "proposed_corrected_classification": corrected_job,
            "is_true_roofing": true_roofing,
            "true_roofing_reason": true_roofing_reason,
            "contractor_attached": contractor_attached,
            "is_owner_builder": is_owner_builder,
            "is_actual_sales_opportunity": sales_opportunity,
            "opportunity_type": opportunity_type,
            "opportunity_reason": opportunity_reason,
            "duplicate_or_related_project": related_project or "Single Independent Permit",
        }

    def compute_audit_metrics(self, audited_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculates commercial precision and conversion statistics."""
        total = len(audited_records)

        true_roofing_count = sum(1 for r in audited_records if r["is_true_roofing"])
        sales_opportunity_count = sum(1 for r in audited_records if r["is_actual_sales_opportunity"])
        contractor_attached_count = sum(1 for r in audited_records if r["contractor_attached"])
        owner_builder_count = sum(1 for r in audited_records if r["is_owner_builder"])
        fresh_count = sum(1 for r in audited_records if r["freshness_tier"] in ["NEW (0-24h)", "FRESH (1-3d)"])

        classification_matches = sum(
            1 for r in audited_records
            if r["current_classification"] == r["proposed_corrected_classification"]
        )

        opportunity_breakdown = {}
        for r in audited_records:
            ot = r["opportunity_type"]
            opportunity_breakdown[ot] = opportunity_breakdown.get(ot, 0) + 1

        freshness_dist = {}
        for r in audited_records:
            ft = r["freshness_tier"]
            freshness_dist[ft] = freshness_dist.get(ft, 0) + 1

        return {
            "total_audited": total,
            "true_roofing_count": true_roofing_count,
            "true_roofing_percentage": round((true_roofing_count / total) * 100, 2),
            "classification_precision": round((classification_matches / total) * 100, 2),
            "actual_sales_opportunity_count": sales_opportunity_count,
            "actual_sales_opportunity_percentage": round((sales_opportunity_count / total) * 100, 2),
            "contractor_attached_count": contractor_attached_count,
            "contractor_attached_percentage": round((contractor_attached_count / total) * 100, 2),
            "owner_builder_count": owner_builder_count,
            "owner_builder_percentage": round((owner_builder_count / total) * 100, 2),
            "fresh_less_than_3_days_count": fresh_count,
            "fresh_less_than_3_days_percentage": round((fresh_count / total) * 100, 2),
            "opportunity_breakdown": opportunity_breakdown,
            "freshness_distribution": freshness_dist,
        }

    def export_audit_files(self, records: List[Dict[str, Any]], csv_path: Path, json_path: Path) -> None:
        """Saves full audit records to CSV and JSON."""
        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)

        if records:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=records[0].keys())
                writer.writeheader()
                writer.writerows(records)
