"""
Data models for NOAA / National Weather Service severe weather and storm events.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class StormEvent:
    """Represents a localized severe weather storm event with geographic coordinates."""

    event_id: str
    event_type: str  # e.g., 'HAIL', 'TSTM_WND_DMG', 'TSTM_WND_GST', 'TORNADO', 'TROPICAL_CYCLONE'
    magnitude: Optional[float] = None  # e.g., hail size in inches, wind speed in MPH
    unit: Optional[str] = None  # 'INCHES', 'MPH', 'KTS'
    event_time: str = ""  # ISO 8601 UTC
    latitude: float = 0.0
    longitude: float = 0.0
    city: Optional[str] = None
    county: str = "Miami-Dade"
    remark: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def age_days(self, ref_dt: Optional[datetime] = None) -> Optional[float]:
        """Computes event age in days relative to reference UTC timestamp."""
        if not self.event_time:
            return None
        try:
            ref = ref_dt or datetime.now(timezone.utc)
            dt = datetime.fromisoformat(self.event_time.replace("Z", "+00:00"))
            return max(0.0, round((ref - dt).total_seconds() / 86400.0, 1))
        except Exception:
            return None

    @property
    def is_severe(self) -> bool:
        """Determines if the event meets National Weather Service severe criteria."""
        t = self.event_type.upper()
        if "TORNADO" in t or "TROPICAL" in t or "HURRICANE" in t:
            return True
        if "HAIL" in t:
            return (self.magnitude or 0.0) >= 0.75  # >= 0.75" hail causes shingle bruising
        if "WND" in t or "WIND" in t:
            return (self.magnitude or 0.0) >= 50.0  # >= 50 MPH causes roof edge/shingle uplift
        return False

    @classmethod
    def from_nws_lsr_feature(cls, feat: Dict[str, Any]) -> Optional[StormEvent]:
        """Constructs StormEvent from an NWS Local Storm Report GeoJSON feature."""
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates") if geom else None

        # Coordinates can be in geometry or properties
        lon = coords[0] if coords and len(coords) >= 2 else props.get("lon")
        lat = coords[1] if coords and len(coords) >= 2 else props.get("lat")

        if lat is None or lon is None:
            return None

        try:
            lat = float(lat)
            lon = float(lon)
        except (ValueError, TypeError):
            return None

        raw_type = str(props.get("typetext") or props.get("type") or "UNKNOWN").upper().strip()
        county = str(props.get("county") or "Miami-Dade").strip()
        city = props.get("city")
        valid_time = props.get("valid") or props.get("time") or datetime.now(timezone.utc).isoformat()

        # Parse magnitude
        mag = props.get("magf") or props.get("mag")
        mag_float: Optional[float] = None
        if mag is not None:
            try:
                mag_float = float(mag)
            except (ValueError, TypeError):
                pass

        unit = props.get("unit")
        if not unit:
            if "HAIL" in raw_type:
                unit = "INCHES"
            elif "WND" in raw_type or "WIND" in raw_type:
                unit = "MPH"

        remark = props.get("remark")
        if remark:
            remark = str(remark).strip()

        # Generate deterministic event_id
        hash_input = f"{lat:.4f}:{lon:.4f}:{valid_time}:{raw_type}"
        event_id = "STM-" + hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:12].upper()

        return cls(
            event_id=event_id,
            event_type=raw_type,
            magnitude=mag_float,
            unit=unit,
            event_time=valid_time,
            latitude=lat,
            longitude=lon,
            city=str(city).strip() if city else None,
            county=county,
            remark=remark,
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["is_severe"] = self.is_severe
        return d
