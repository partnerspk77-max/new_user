"""
Data profiling, quality measurement, contractor concentration, duplicate analysis,
and validation sampling for Miami-Dade Roofing Permits.
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sqlite3

from src.storage.database import Database


class RoofingProfiler:
    """Performs deep empirical analysis and metrics reporting on permits data."""

    def __init__(self, db: Database):
        self.db = db

    def profile_fields(self) -> Dict[str, Any]:
        """Profiles distributions across TYPE, CAT, DESC, PROPUSE, APPTYPE, and FFRMLINE."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM permits")
        total_records = cursor.fetchone()[0] or 1

        # 1. TYPE distribution
        cursor.execute("SELECT COALESCE(permit_type, '(NULL)') as val, COUNT(*) as c FROM permits GROUP BY permit_type ORDER BY c DESC")
        type_dist = [{"value": r["val"], "count": r["c"], "percentage": round((r["c"] / total_records) * 100, 2)} for r in cursor.fetchall()]

        # 2. Categories & Descriptions (CAT1 - CAT10 unrolled)
        cat_unions = " UNION ALL ".join(
            f"SELECT category_{i} as cat, description_{i} as desc FROM permits WHERE category_{i} IS NOT NULL"
            for i in range(1, 11)
        )
        cursor.execute(
            f"""
            SELECT cat, COALESCE(desc, '(EMPTY)') as desc, COUNT(*) as c
            FROM ({cat_unions})
            GROUP BY cat, desc
            ORDER BY c DESC
            """
        )
        cat_desc_dist = [
            {"category": r["cat"], "description": r["desc"], "count": r["c"], "percentage": round((r["c"] / total_records) * 100, 2)}
            for r in cursor.fetchall()
        ]

        # 3. Roofing Specific Categories
        roofing_cats = [c for c in cat_desc_dist if c["category"] in ["0092", "0095", "0096", "0107", "0109", "0050", "0106"]]

        # 4. Proposed Use (Top 15)
        cursor.execute(
            """
            SELECT COALESCE(proposed_use, '(NULL)') as val, COUNT(*) as c
            FROM permits GROUP BY proposed_use ORDER BY c DESC LIMIT 15
            """
        )
        prop_use_dist = [{"value": r["val"], "count": r["c"], "percentage": round((r["c"] / total_records) * 100, 2)} for r in cursor.fetchall()]

        # 5. Application Type (Top 10)
        cursor.execute(
            """
            SELECT COALESCE(application_type, '(NULL)') as val, COUNT(*) as c
            FROM permits GROUP BY application_type ORDER BY c DESC LIMIT 10
            """
        )
        app_type_dist = [{"value": r["val"], "count": r["c"], "percentage": round((r["c"] / total_records) * 100, 2)} for r in cursor.fetchall()]

        # 6. Common Roofing Terms in FFRMLINE (comment)
        cursor.execute("SELECT comment FROM permits WHERE comment IS NOT NULL")
        comments = [r[0].upper() for r in cursor.fetchall()]
        terms = ["ROOF", "REROOF", "RE-ROOF", "SHINGLE", "TILE", "METAL", "FLAT", "SOLAR", "REPAIR", "LEAK", "WATERPROOF", "ALUMIN"]
        term_counts = []
        for t in terms:
            cnt = sum(1 for c in comments if t in c)
            term_counts.append({"term": t, "matching_records": cnt, "percentage": round((cnt / total_records) * 100, 2)})
        term_counts.sort(key=lambda x: x["matching_records"], reverse=True)

        return {
            "total_records": total_records,
            "type_distribution": type_dist,
            "top_categories": cat_desc_dist[:25],
            "roofing_categories": roofing_cats,
            "proposed_use_distribution": prop_use_dist,
            "application_type_distribution": app_type_dist,
            "common_roofing_terms": term_counts,
        }

    def analyze_quality(self) -> Dict[str, Any]:
        """Measures data completeness, dates, and status for confirmed roofing permits."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM roofing_permits")
        total_roofing = cursor.fetchone()[0]

        if total_roofing == 0:
            return {"error": "No roofing permits classified yet. Run classify-roofing first."}

        # Status distribution
        cursor.execute("SELECT COALESCE(status, '(NULL)') as val, COUNT(*) as c FROM roofing_permits GROUP BY status ORDER BY c DESC")
        status_dist = [{"status": r["val"], "count": r["c"], "percentage": round((r["c"] / total_roofing) * 100, 2)} for r in cursor.fetchall()]

        # Residential vs Commercial
        cursor.execute("SELECT COALESCE(residential_commercial, '(NULL)') as val, COUNT(*) as c FROM roofing_permits GROUP BY residential_commercial ORDER BY c DESC")
        rescomm_dist = [{"type": r["val"], "count": r["c"], "percentage": round((r["c"] / total_roofing) * 100, 2)} for r in cursor.fetchall()]

        # Completeness fill rates
        cursor.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN folio IS NOT NULL AND TRIM(folio) != '' THEN 1 ELSE 0 END) as with_folio,
                SUM(CASE WHEN contractor_name IS NOT NULL AND TRIM(contractor_name) != '' THEN 1 ELSE 0 END) as with_contractor,
                SUM(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL THEN 1 ELSE 0 END) as with_geometry,
                MIN(issued_at) as min_date,
                MAX(issued_at) as max_date
            FROM roofing_permits
            """
        )
        comp = dict(cursor.fetchone())

        # Records per week (recent coverage)
        cursor.execute(
            """
            SELECT SUBSTR(issued_at, 1, 7) as month, COUNT(*) as c
            FROM roofing_permits WHERE issued_at IS NOT NULL
            GROUP BY month ORDER BY month DESC
            """
        )
        monthly_counts = [{"month": r["month"], "count": r["c"]} for r in cursor.fetchall()]

        return {
            "total_roofing_permits": total_roofing,
            "status_distribution": status_dist,
            "rescomm_distribution": rescomm_dist,
            "folio_fill_rate": round((comp["with_folio"] / total_roofing) * 100, 2),
            "contractor_fill_rate": round((comp["with_contractor"] / total_roofing) * 100, 2),
            "geometry_fill_rate": round((comp["with_geometry"] / total_roofing) * 100, 2),
            "oldest_permit_date": comp["min_date"],
            "newest_permit_date": comp["max_date"],
            "monthly_coverage": monthly_counts,
        }

    def analyze_contractors(self, top_n: int = 20) -> List[Dict[str, Any]]:
        """Analyzes contractor concentration and activity metrics for roofing jobs."""
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            f"""
            SELECT
                COALESCE(contractor_name, '(UNASSIGNED/OWNER-BUILDER)') as contractor_name,
                COUNT(*) as permit_count,
                SUM(CASE WHEN status = 'A' THEN 1 ELSE 0 END) as active_count,
                SUM(CASE WHEN status = 'F' THEN 1 ELSE 0 END) as finalized_count,
                MIN(issued_at) as first_permit_date,
                MAX(issued_at) as latest_permit_date
            FROM roofing_permits
            GROUP BY contractor_name
            ORDER BY permit_count DESC
            LIMIT {top_n}
            """
        )
        return [dict(r) for r in cursor.fetchall()]

    def analyze_duplicate_and_project_patterns(self) -> Dict[str, Any]:
        """
        Analyzes multi-permit folios and clusters (same folio + similar scope + near dates)
        to distinguish multi-phase projects from potential duplicate submissions.
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM roofing_permits")
        total_roofing = cursor.fetchone()[0]

        # 1. Properties with multiple roofing permits
        cursor.execute(
            """
            SELECT folio, COUNT(*) as c, GROUP_CONCAT(permit_number, '; ') as permits,
                   GROUP_CONCAT(comment, '; ') as comments,
                   GROUP_CONCAT(SUBSTR(issued_at, 1, 10), '; ') as dates
            FROM roofing_permits
            WHERE folio IS NOT NULL AND TRIM(folio) != ''
            GROUP BY folio
            HAVING c > 1
            ORDER BY c DESC
            """
        )
        multi_folios = [dict(r) for r in cursor.fetchall()]

        # 2. Same folio issued on the exact same date (cluster / split permits)
        cursor.execute(
            """
            SELECT folio, SUBSTR(issued_at, 1, 10) as issue_day, COUNT(*) as c,
                   GROUP_CONCAT(permit_number, '; ') as permits,
                   GROUP_CONCAT(roofing_job_type, '; ') as job_types
            FROM roofing_permits
            WHERE folio IS NOT NULL AND issued_at IS NOT NULL
            GROUP BY folio, issue_day
            HAVING c > 1
            ORDER BY c DESC
            """
        )
        same_day_clusters = [dict(r) for r in cursor.fetchall()]

        return {
            "total_roofing_permits": total_roofing,
            "folios_with_multiple_permits_count": len(multi_folios),
            "same_day_clustered_projects_count": len(same_day_clusters),
            "multi_permit_samples": multi_folios[:10],
            "same_day_cluster_samples": same_day_clusters[:10],
        }

    def generate_validation_sample(
        self,
        sample_size_per_class: int = 50,
        random_seed: int = 42,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Extracts a stratified validation set:
        - 50 predicted roofing records
        - 50 predicted non-roofing records
        - 50 ambiguous records
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        # 1. Confirmed Roofing (from roofing_permits where job_type != 'AMBIGUOUS')
        cursor.execute(
            """
            SELECT process_number, permit_number, address, permit_type,
                   category_1, description_1, category_2, description_2,
                   proposed_use, comment, roofing_job_type, roofing_confidence,
                   classification_reason
            FROM roofing_permits
            WHERE roofing_job_type != 'AMBIGUOUS'
            """
        )
        all_roofing = [dict(r) for r in cursor.fetchall()]

        # 2. Ambiguous Records (from roofing_permits where job_type == 'AMBIGUOUS' or confidence < 0.8)
        cursor.execute(
            """
            SELECT process_number, permit_number, address, permit_type,
                   category_1, description_1, category_2, description_2,
                   proposed_use, comment, roofing_job_type, roofing_confidence,
                   classification_reason
            FROM roofing_permits
            WHERE roofing_job_type = 'AMBIGUOUS' OR roofing_confidence < 0.80
            """
        )
        all_ambiguous = [dict(r) for r in cursor.fetchall()]

        # 3. Non-Roofing Records (permits not in roofing_permits)
        cursor.execute(
            """
            SELECT process_number, permit_number, address, permit_type,
                   category_1, description_1, category_2, description_2,
                   proposed_use, comment, 'NOT_ROOFING' as roofing_job_type, 0.99 as roofing_confidence,
                   'Non-roofing trade category (Plumbing, Mechanical, Electrical, etc.)' as classification_reason
            FROM permits
            WHERE id NOT IN (SELECT permit_id FROM roofing_permits)
            """
        )
        all_non_roofing = [dict(r) for r in cursor.fetchall()]

        rnd = random.Random(random_seed)

        sampled_roofing = rnd.sample(all_roofing, min(sample_size_per_class, len(all_roofing)))
        sampled_non_roofing = rnd.sample(all_non_roofing, min(sample_size_per_class, len(all_non_roofing)))
        sampled_ambiguous = rnd.sample(all_ambiguous, min(sample_size_per_class, len(all_ambiguous)))

        return {
            "predicted_roofing": sampled_roofing,
            "predicted_non_roofing": sampled_non_roofing,
            "ambiguous": sampled_ambiguous,
        }

    def export_validation_sample(
        self,
        json_path: Path,
        csv_path: Path,
        sample_size: int = 50,
    ) -> None:
        """Saves the 50/50/50 validation set to JSON and CSV for human audit."""
        samples = self.generate_validation_sample(sample_size_per_class=sample_size)

        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(samples, f, indent=2)

        # Flatten into one CSV with a 'stratum' column
        flat_rows = []
        for stratum_name, records in samples.items():
            for r in records:
                flat_rows.append({"stratum": stratum_name, **r})

        csv_path.parent.mkdir(parents=True, exist_ok=True)
        if flat_rows:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=flat_rows[0].keys())
                writer.writeheader()
                writer.writerows(flat_rows)
