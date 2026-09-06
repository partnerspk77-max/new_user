"""
Discovery inspection tooling for Miami-Dade Building Permits.
Answers the 9 key exploratory questions (A through I) to analyze actual data distributions:
- TYPE values
- CAT1-CAT10 values
- Most common DESC1-DESC10 values
- Most recent ISSUDATE values
- Records per day
- Active / Expired / Finalized status counts
- % with FOLIO
- % with contractor
- % with geometry
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from src.client.arcgis_client import ArcGISClient
from src.logger import logger
from src.storage.database import Database


class PermitInspector:
    """Provides statistical inspection reports on ingested permits and remote schema."""

    def __init__(self, db: Database, client: Optional[ArcGISClient] = None):
        self.db = db
        self.client = client

    def inspect_local(self) -> Dict[str, Any]:
        """
        Runs comprehensive discovery queries against the local normalized SQLite database.
        Returns a structured dictionary of metrics.
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        # Check total count
        cursor.execute("SELECT COUNT(*) FROM permits")
        total_records = cursor.fetchone()[0]

        if total_records == 0:
            return {"error": "Database contains 0 records. Run backfill or sync first."}

        # A. Distinct TYPE values
        cursor.execute(
            """
            SELECT COALESCE(permit_type, '(NULL)') AS val, COUNT(*) AS count
            FROM permits
            GROUP BY permit_type
            ORDER BY count DESC
            """
        )
        type_values = [dict(r) for r in cursor.fetchall()]

        # B. Distinct CAT1-CAT10 values
        cat_unions = " UNION ALL ".join(
            f"SELECT category_{i} AS cat FROM permits WHERE category_{i} IS NOT NULL AND category_{i} != ''"
            for i in range(1, 11)
        )
        cursor.execute(
            f"""
            SELECT cat, COUNT(*) as count
            FROM ({cat_unions})
            GROUP BY cat
            ORDER BY count DESC
            """
        )
        categories = [dict(r) for r in cursor.fetchall()]

        # C. Most common DESC1-DESC10 values (top 25)
        desc_unions = " UNION ALL ".join(
            f"SELECT description_{i} AS description FROM permits WHERE description_{i} IS NOT NULL AND description_{i} != ''"
            for i in range(1, 11)
        )
        cursor.execute(
            f"""
            SELECT description, COUNT(*) as count
            FROM ({desc_unions})
            GROUP BY description
            ORDER BY count DESC
            LIMIT 25
            """
        )
        descriptions = [dict(r) for r in cursor.fetchall()]

        # D. Most recent ISSUDATE values (top 10 latest individual dates)
        cursor.execute(
            """
            SELECT DISTINCT issued_at
            FROM permits
            WHERE issued_at IS NOT NULL
            ORDER BY issued_at DESC
            LIMIT 10
            """
        )
        recent_issue_dates = [r[0] for r in cursor.fetchall()]

        # E. Records per day (last 15 days present in data)
        cursor.execute(
            """
            SELECT SUBSTR(issued_at, 1, 10) AS day, COUNT(*) AS count
            FROM permits
            WHERE issued_at IS NOT NULL
            GROUP BY day
            ORDER BY day DESC
            LIMIT 15
            """
        )
        records_per_day = [dict(r) for r in cursor.fetchall()]

        # F. BPSTATUS distribution (Active, Expired, Finalized, etc.)
        cursor.execute(
            """
            SELECT COALESCE(status, '(NULL)') AS status, COUNT(*) AS count
            FROM permits
            GROUP BY status
            ORDER BY count DESC
            """
        )
        status_counts = [dict(r) for r in cursor.fetchall()]

        # G, H, I: Completeness Percentages
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN folio IS NOT NULL AND TRIM(folio) != '' THEN 1 ELSE 0 END) AS with_folio,
                SUM(CASE WHEN (contractor_name IS NOT NULL AND TRIM(contractor_name) != '')
                              OR (contractor_number IS NOT NULL AND TRIM(contractor_number) != '') THEN 1 ELSE 0 END) AS with_contractor,
                SUM(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL THEN 1 ELSE 0 END) AS with_geometry
            FROM permits
            """
        )
        comp_row = dict(cursor.fetchone())
        total = comp_row["total"] or 1

        folio_pct = round((comp_row["with_folio"] / total) * 100, 2)
        contr_pct = round((comp_row["with_contractor"] / total) * 100, 2)
        geom_pct = round((comp_row["with_geometry"] / total) * 100, 2)

        return {
            "total_permits": total_records,
            "A_permit_types": type_values,
            "B_categories": categories,
            "C_top_descriptions": descriptions,
            "D_recent_issue_dates": recent_issue_dates,
            "E_records_per_day": records_per_day,
            "F_status_distribution": status_counts,
            "G_folio_percentage": folio_pct,
            "H_contractor_percentage": contr_pct,
            "I_geometry_percentage": geom_pct,
        }

    def print_report(self, stats: Dict[str, Any]) -> None:
        """Renders the discovery inspection report to standard output."""
        if "error" in stats:
            print(f"\n[!] {stats['error']}\n")
            return

        print("\n" + "=" * 70)
        print("  MIAMI-DADE COUNTY PERMITS: DISCOVERY & SCHEMA INSPECTION REPORT")
        print("=" * 70)
        print(f"Total Ingested Records in Database: {stats['total_permits']:,}\n")

        print("--- [G, H, I] Data Completeness ---")
        print(f"  * Folio Fill Rate:       {stats['G_folio_percentage']}%")
        print(f"  * Contractor Fill Rate:  {stats['H_contractor_percentage']}%")
        print(f"  * Geometry Fill Rate:    {stats['I_geometry_percentage']}%\n")

        print("--- [F] Permit Status Distribution (BPSTATUS) ---")
        for s in stats["F_status_distribution"]:
            status_desc = {"A": "Active", "E": "Expired", "F": "Finalized"}.get(s["status"], "Other")
            print(f"  {s['status']:<6} ({status_desc:<9}): {s['count']:,}")
        print()

        print("--- [A] Permit Types (TYPE) ---")
        for t in stats["A_permit_types"][:10]:
            print(f"  {t['val']:<20}: {t['count']:,}")
        if len(stats["A_permit_types"]) > 10:
            print(f"  ... ({len(stats['A_permit_types']) - 10} more types)")
        print()

        print("--- [B] Category Codes (CAT1 - CAT10) ---")
        for c in stats["B_categories"][:15]:
            print(f"  {c['cat']:<15}: {c['count']:,}")
        if len(stats["B_categories"]) > 15:
            print(f"  ... ({len(stats['B_categories']) - 15} more categories)")
        print()

        print("--- [C] Top Descriptions (DESC1 - DESC10) ---")
        for d in stats["C_top_descriptions"][:15]:
            desc_text = (d["description"][:50] + "...") if len(d["description"]) > 50 else d["description"]
            print(f"  {desc_text:<53}: {d['count']:,}")
        print()

        print("--- [D] Most Recent Issue Dates ---")
        for d in stats["D_recent_issue_dates"]:
            print(f"  * {d}")
        print()

        print("--- [E] Records Per Day (Recent Sample) ---")
        for r in stats["E_records_per_day"][:10]:
            print(f"  {r['day']}: {r['count']:,} permits")
        print("=" * 70 + "\n")
