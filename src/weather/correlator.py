"""
Geospatial Storm Correlator.
Computes Haversine geodesic proximity between property locations and NOAA/NWS storm events.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.weather.models import StormEvent


EARTH_RADIUS_MILES = 3958.8


def haversine_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two points in statute miles."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return round(EARTH_RADIUS_MILES * c, 2)


class StormCorrelator:
    """Matches property coordinates to nearest and most severe weather events."""

    def __init__(self, storm_events: List[StormEvent], max_distance_miles: float = 15.0):
        self.storm_events = [e for e in storm_events if e.latitude and e.longitude]
        self.max_distance_miles = max_distance_miles

    def find_nearest_storm(
        self,
        lat: Optional[float],
        lon: Optional[float],
        ref_dt: Optional[datetime] = None,
    ) -> Optional[Tuple[StormEvent, float, float]]:
        """
        Finds the nearest severe storm event for a property.
        Returns: Tuple of (StormEvent, distance_miles, age_days) or None if outside max radius.
        """
        if lat is None or lon is None or not self.storm_events:
            return None

        best_event: Optional[StormEvent] = None
        min_dist = float("inf")
        ref = ref_dt or datetime.now(timezone.utc)

        for ev in self.storm_events:
            dist = haversine_distance_miles(lat, lon, ev.latitude, ev.longitude)
            if dist <= self.max_distance_miles:
                # Prioritize severe events (Hail, Tornado, Wind Damage) and closer events
                if dist < min_dist:
                    min_dist = dist
                    best_event = ev

        if best_event is not None:
            age_days = best_event.age_days(ref_dt=ref) or 0.0
            return (best_event, min_dist, age_days)

        return None
