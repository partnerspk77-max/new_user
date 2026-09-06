"""
Storage repository for Miami-Dade Property Appraiser (PAPA) parcel intelligence.
Persists parcel physical characteristics, year built, square footage, and ownership.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from src.enrichment.models import PropertyParcel
from src.logger import logger
from src.storage.database import Database


class ParcelStorage:
    """Enterprise SQLite repository for property appraiser parcel data."""

    def __init__(self, db: Database):
        self.db = db
        self.init_schema()

    def init_schema(self) -> None:
        """Initializes parcel tables and indexes."""
        conn = self.db.get_connection()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS property_parcels (
                    folio TEXT PRIMARY KEY,
                    year_built INTEGER,
                    building_actual_area REAL,
                    building_heated_area REAL,
                    bedroom_count INTEGER,
                    bathroom_count REAL,
                    dor_code TEXT,
                    dor_desc TEXT,
                    owner_name TEXT,
                    site_address TEXT,
                    site_zip TEXT,
                    mailing_address TEXT,
                    assessed_value REAL,
                    is_vacant INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_parcel_year ON property_parcels (year_built);
                CREATE INDEX IF NOT EXISTS idx_parcel_dor ON property_parcels (dor_desc);
                CREATE INDEX IF NOT EXISTS idx_parcel_val ON property_parcels (assessed_value DESC);
                """
            )

    def upsert_parcels(self, parcels: List[PropertyParcel]) -> int:
        """
        Inserts or updates property parcels statefully.
        Returns number of records saved.
        """
        if not parcels:
            return 0

        now_iso = datetime.now(timezone.utc).isoformat()
        saved = 0
        conn = self.db.get_connection()

        with conn:
            for p in parcels:
                clean_folio = p.folio.replace("-", "").strip()
                if not clean_folio:
                    continue

                conn.execute(
                    """
                    INSERT INTO property_parcels (
                        folio, year_built, building_actual_area, building_heated_area,
                        bedroom_count, bathroom_count, dor_code, dor_desc, owner_name,
                        site_address, site_zip, mailing_address, assessed_value, is_vacant,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(folio) DO UPDATE SET
                        year_built = excluded.year_built,
                        building_actual_area = excluded.building_actual_area,
                        building_heated_area = excluded.building_heated_area,
                        bedroom_count = excluded.bedroom_count,
                        bathroom_count = excluded.bathroom_count,
                        dor_code = excluded.dor_code,
                        dor_desc = excluded.dor_desc,
                        owner_name = excluded.owner_name,
                        site_address = excluded.site_address,
                        site_zip = excluded.site_zip,
                        mailing_address = excluded.mailing_address,
                        assessed_value = excluded.assessed_value,
                        is_vacant = excluded.is_vacant,
                        updated_at = excluded.updated_at
                    """,
                    (
                        clean_folio,
                        p.year_built,
                        p.building_actual_area,
                        p.building_heated_area,
                        p.bedroom_count,
                        p.bathroom_count,
                        p.dor_code,
                        p.dor_desc,
                        p.owner_name,
                        p.site_address,
                        p.site_zip,
                        p.mailing_address,
                        p.assessed_value,
                        1 if p.is_vacant else 0,
                        p.created_at or now_iso,
                        now_iso,
                    ),
                )
                saved += 1

        logger.info(f"Successfully upserted {saved} parcel records into property_parcels.")
        return saved

    def get_parcel(self, folio: str) -> Optional[PropertyParcel]:
        """Retrieves a single parcel by clean folio."""
        clean_folio = folio.replace("-", "").strip()
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT folio, year_built, building_actual_area, building_heated_area,
                   bedroom_count, bathroom_count, dor_code, dor_desc, owner_name,
                   site_address, site_zip, mailing_address, assessed_value, is_vacant,
                   created_at, updated_at
            FROM property_parcels WHERE folio = ?
            """,
            (clean_folio,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_parcel(row)

    def get_parcels_map(self, folios: List[str]) -> Dict[str, PropertyParcel]:
        """Returns a mapping of clean_folio -> PropertyParcel for requested folios."""
        if not folios:
            return {}

        clean_map = {f.replace("-", "").strip(): f for f in folios if f}
        clean_keys = list(clean_map.keys())
        results: Dict[str, PropertyParcel] = {}

        chunk_size = 500
        conn = self.db.get_connection()
        cur = conn.cursor()
        for i in range(0, len(clean_keys), chunk_size):
            chunk = clean_keys[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(
                f"""
                SELECT folio, year_built, building_actual_area, building_heated_area,
                       bedroom_count, bathroom_count, dor_code, dor_desc, owner_name,
                       site_address, site_zip, mailing_address, assessed_value, is_vacant,
                       created_at, updated_at
                FROM property_parcels WHERE folio IN ({placeholders})
                """,
                chunk,
            )
            for row in cur.fetchall():
                p = self._row_to_parcel(row)
                results[p.folio] = p

        return results

    def get_all_parcels_map(self) -> Dict[str, PropertyParcel]:
        """Returns a dictionary of all cached parcels keyed by clean folio."""
        results: Dict[str, PropertyParcel] = {}
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT folio, year_built, building_actual_area, building_heated_area,
                   bedroom_count, bathroom_count, dor_code, dor_desc, owner_name,
                   site_address, site_zip, mailing_address, assessed_value, is_vacant,
                   created_at, updated_at
            FROM property_parcels
            """
        )
        for row in cur.fetchall():
            p = self._row_to_parcel(row)
            results[p.folio] = p
        return results

    def get_unenriched_folios(self, target_folios: List[str]) -> List[str]:
        """Finds target folios that do not yet exist in property_parcels table."""
        if not target_folios:
            return []

        clean_targets = list({f.replace("-", "").strip() for f in target_folios if f and f.strip()})
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT folio FROM property_parcels")
        cached_folios: Set[str] = {r[0] for r in cur.fetchall()}

        return [f for f in clean_targets if f not in cached_folios]

    def count_parcels(self) -> int:
        """Returns total cached parcels in database."""
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM property_parcels")
        return int(cur.fetchone()[0])

    def get_stats(self) -> Dict[str, Any]:
        """Returns statistical distribution of parcels."""
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM property_parcels")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM property_parcels WHERE year_built IS NOT NULL AND year_built > 0")
        with_year = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM property_parcels WHERE building_actual_area > 0")
        with_area = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM property_parcels WHERE owner_name IS NOT NULL")
        with_owner = cur.fetchone()[0]

        cur.execute("SELECT AVG(year_built) FROM property_parcels WHERE year_built > 1900")
        avg_year = cur.fetchone()[0]

        return {
            "total_parcels": total,
            "parcels_with_year_built": with_year,
            "parcels_with_area": with_area,
            "parcels_with_owner": with_owner,
            "avg_year_built": round(avg_year, 1) if avg_year else None,
        }

    @staticmethod
    def _row_to_parcel(row: Any) -> PropertyParcel:
        return PropertyParcel(
            folio=row[0],
            year_built=row[1],
            building_actual_area=row[2],
            building_heated_area=row[3],
            bedroom_count=row[4],
            bathroom_count=row[5],
            dor_code=row[6],
            dor_desc=row[7],
            owner_name=row[8],
            site_address=row[9],
            site_zip=row[10],
            mailing_address=row[11],
            assessed_value=row[12],
            is_vacant=bool(row[13]),
            created_at=row[14],
            updated_at=row[15],
        )
