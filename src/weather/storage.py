"""
Storage repository for NOAA / NWS severe weather and storm reports.
Persists event locations, timestamps, event types, and storm severity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.logger import logger
from src.storage.database import Database
from src.weather.models import StormEvent


class StormStorage:
    """Enterprise SQLite repository for severe storm reports and weather hazards."""

    def __init__(self, db: Database):
        self.db = db
        self.init_schema()

    def init_schema(self) -> None:
        """Initializes storm_events table and spatial indexes."""
        conn = self.db.get_connection()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS storm_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    magnitude REAL,
                    unit TEXT,
                    event_time TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    city TEXT,
                    county TEXT NOT NULL,
                    remark TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_storm_time ON storm_events (event_time);
                CREATE INDEX IF NOT EXISTS idx_storm_type ON storm_events (event_type);
                CREATE INDEX IF NOT EXISTS idx_storm_geo ON storm_events (latitude, longitude);
                """
            )

    def upsert_storm_events(self, events: List[StormEvent]) -> int:
        """Statefully persists storm reports, skipping exact duplicates."""
        if not events:
            return 0

        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self.db.get_connection()
        saved = 0

        with conn:
            for ev in events:
                conn.execute(
                    """
                    INSERT INTO storm_events (
                        event_id, event_type, magnitude, unit, event_time,
                        latitude, longitude, city, county, remark, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        magnitude = excluded.magnitude,
                        unit = excluded.unit,
                        remark = excluded.remark
                    """,
                    (
                        ev.event_id,
                        ev.event_type,
                        ev.magnitude,
                        ev.unit,
                        ev.event_time,
                        ev.latitude,
                        ev.longitude,
                        ev.city,
                        ev.county,
                        ev.remark,
                        ev.created_at or now_iso,
                    ),
                )
                saved += 1

        logger.info(f"Successfully upserted {saved} storm events into storm_events table.")
        return saved

    def get_all_storm_events(self) -> List[StormEvent]:
        """Returns all recorded storm events ordered chronologically descending."""
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT event_id, event_type, magnitude, unit, event_time,
                   latitude, longitude, city, county, remark, created_at
            FROM storm_events
            ORDER BY event_time DESC
            """
        )
        return [self._row_to_event(r) for r in cur.fetchall()]

    def count_events(self) -> int:
        """Returns count of recorded storm events."""
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM storm_events")
        return int(cur.fetchone()[0])

    def get_stats(self) -> Dict[str, Any]:
        """Returns distribution of events by type and county."""
        conn = self.db.get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM storm_events")
        total = cur.fetchone()[0]

        cur.execute(
            """
            SELECT event_type, COUNT(*) as c
            FROM storm_events GROUP BY event_type ORDER BY c DESC
            """
        )
        by_type = {r["event_type"]: r["c"] for r in cur.fetchall()}

        cur.execute("SELECT MIN(event_time), MAX(event_time) FROM storm_events")
        row = cur.fetchone()
        min_date, max_date = (row[0], row[1]) if row else (None, None)

        return {
            "total_events": total,
            "by_type": by_type,
            "earliest_event": min_date,
            "latest_event": max_date,
        }

    @staticmethod
    def _row_to_event(row: Any) -> StormEvent:
        return StormEvent(
            event_id=row[0],
            event_type=row[1],
            magnitude=row[2],
            unit=row[3],
            event_time=row[4],
            latitude=row[5],
            longitude=row[6],
            city=row[7],
            county=row[8],
            remark=row[9],
            created_at=row[10],
        )
