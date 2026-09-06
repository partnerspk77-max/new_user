"""
Domain models for Property Evidence Graph and Stateful Commercial Signals.
Implements the Signal -> Evidence -> Opportunity -> Verified Lead mental model.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class SignalType:
    PROPERTY_RENOVATION_SIGNAL = "PROPERTY_RENOVATION_SIGNAL"
    SOLAR_ROOF_SIGNAL = "SOLAR_ROOF_SIGNAL"
    OWNER_BUILDER_ROOF_SIGNAL = "OWNER_BUILDER_ROOF_SIGNAL"
    NEW_ROOF_PERMIT = "NEW_ROOF_PERMIT"
    ACTIVE_ROOF_PROJECT = "ACTIVE_ROOF_PROJECT"


class SignalStatus:
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


@dataclass
class PropertyTimeline:
    """Aggregated multi-trade timeline for a single property parcel (Folio)."""
    folio: str
    address: Optional[str] = None
    total_permits: int = 0
    trades: List[str] = field(default_factory=list)
    active_trades: List[str] = field(default_factory=list)
    roof_permits_count: int = 0
    has_active_roof_permit: bool = False
    last_roof_permit_date: Optional[str] = None
    years_since_last_roof_permit: Optional[float] = None
    last_roof_system: Optional[str] = None  # e.g., 'SHINGLE', 'TILE', 'COMMERCIAL_FLAT', 'METAL'
    last_roof_contractor: Optional[str] = None
    has_active_non_roof_permit: bool = False
    non_roof_renovation_types: List[str] = field(default_factory=list)
    contractors_seen: List[str] = field(default_factory=list)
    residential_commercial: Optional[str] = None
    first_permit_date: Optional[str] = None
    latest_permit_date: Optional[str] = None
    permits: List[Dict[str, Any]] = field(default_factory=list)
    # Physical Parcel Attributes (from Property Appraiser PaGISView)
    year_built: Optional[int] = None
    building_actual_area: Optional[float] = None
    building_heated_area: Optional[float] = None
    dor_desc: Optional[str] = None
    owner_name: Optional[str] = None
    assessed_value: Optional[float] = None

    @property
    def building_age(self) -> Optional[int]:
        if self.year_built is not None and 1800 <= self.year_built <= datetime.now(timezone.utc).year:
            return datetime.now(timezone.utc).year - self.year_built
        return None

    @property
    def year_built_status(self) -> str:
        """Explicitly tracks whether year built is verified from official records or unrecorded."""
        return "VERIFIED" if (self.year_built is not None and self.year_built > 0) else "UNRECORDED"

    @property
    def roof_history_status(self) -> str:
        """Epistemic status: distinguishes verified prior permits from absence of permits in window."""
        return "VERIFIED_PRIOR_PERMIT" if self.last_roof_permit_date else "NO_PERMIT_IN_DATASET_WINDOW"

    @property
    def estimated_roof_squares(self) -> Optional[float]:
        area = self.building_actual_area or self.building_heated_area
        if area and area > 0:
            return round(area / 100.0, 1)
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["building_age"] = self.building_age
        d["year_built_status"] = self.year_built_status
        d["roof_history_status"] = self.roof_history_status
        d["estimated_roof_squares"] = self.estimated_roof_squares
        return d


@dataclass
class PropertySignal:
    """
    Represents an evidence-backed commercial signal with identity and lifecycle.
    Distinguishes verifiable factual signals from speculative lead claims.
    """
    signal_id: str
    folio: str
    address: Optional[str]
    signal_type: str
    target_audience: str  # 'ROOFING_CONTRACTOR' or 'SUPPLIER_DISTRIBUTOR'
    status: str = SignalStatus.ACTIVE  # 'ACTIVE', 'RESOLVED', 'EXPIRED'
    first_detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_seen_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    evidence_score: float = 0.0  # Transparent 0.0 - 100.0 score
    evidence_breakdown: Dict[str, float] = field(default_factory=dict)
    corroborating_signals: List[str] = field(default_factory=list)
    unverified_assumptions: List[str] = field(default_factory=list)
    freshness_tier: str = "UNKNOWN"
    age_hours: Optional[float] = None
    age_days: Optional[float] = None
    contractor_present: bool = False
    contractor_name: Optional[str] = None
    trigger_trade: str = "UNKNOWN"
    trigger_event: str = ""
    recommended_action: str = ""
    residential_commercial: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # Physical Parcel Enrichment Attributes
    year_built: Optional[int] = None
    building_age: Optional[int] = None
    year_built_status: str = "UNRECORDED"  # 'VERIFIED' or 'UNRECORDED'
    roof_history_status: str = "NO_PERMIT_IN_DATASET_WINDOW"  # 'VERIFIED_PRIOR_PERMIT' or 'NO_PERMIT_IN_DATASET_WINDOW'
    building_actual_area: Optional[float] = None
    estimated_roof_squares: Optional[float] = None
    owner_name: Optional[str] = None
    dor_desc: Optional[str] = None
    assessed_value: Optional[float] = None
    # NOAA Storm Proximity Attributes
    storm_event_type: Optional[str] = None
    storm_distance_miles: Optional[float] = None
    storm_event_date: Optional[str] = None
    storm_age_days: Optional[float] = None
    storm_magnitude: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Backward-compatibility alias for opportunity_id / opportunity_type / priority_score
    @property
    def opportunity_id(self) -> str:
        return self.signal_id

    @property
    def opportunity_type(self) -> str:
        return self.signal_type

    @property
    def priority_score(self) -> float:
        return self.evidence_score

    def to_customer_dossier(self) -> Dict[str, Any]:
        """
        Formats opportunity into explicit OBSERVED, INFERENCE, and UNKNOWN blocks
        for credible, enterprise-grade customer presentations.
        """
        observed = []
        if self.year_built:
            observed.append(f"Building built: {self.year_built} (Age: {self.building_age} years)")
        else:
            observed.append("Building construction year: Unrecorded in county appraiser cache")

        if self.roof_history_status == "VERIFIED_PRIOR_PERMIT":
            observed.append("Prior roof permit: On record in municipal database")
        else:
            observed.append("Last roof permit found: None in recent dataset window")

        if self.storm_event_type and self.storm_distance_miles is not None:
            storm_str = f"NWS severe weather: {self.storm_event_type} {self.storm_distance_miles} miles away"
            if self.storm_age_days is not None:
                storm_str += f" ({int(self.storm_age_days)} days ago)"
            observed.append(storm_str)

        for c in self.corroborating_signals:
            if not any(k in c for k in ("Structure built", "NWS", "Recent severe")):
                observed.append(c)

        inferences = [
            f"Opportunity hypothesis: {self.recommended_action or 'Elevated probability of near-term roof work'}",
            f"Signal Classification: {self.signal_type} (Evidence Score: {self.evidence_score:.1f}/100)",
        ]
        if self.estimated_roof_squares:
            est_val = self.estimated_roof_squares * 500.0
            inferences.append(f"Estimated project scale: ~{self.estimated_roof_squares:.1f} roof squares (~${est_val:,.0f} est. contract value)")

        unknowns = [
            "Physical roof covering condition uninspected on-site",
            "Homeowner intent to reroof unverified (requires sales contact)",
            "Insurance claim status unverified",
        ]

        return {
            "signal_id": self.signal_id,
            "folio": self.folio,
            "address": self.address,
            "owner_name": self.owner_name,
            "dor_desc": self.dor_desc,
            "observed": observed,
            "inference": inferences,
            "unknown": unknowns,
        }

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["opportunity_id"] = self.opportunity_id
        d["opportunity_type"] = self.opportunity_type
        d["priority_score"] = self.priority_score
        return d


# Backward compatibility alias
CommercialOpportunity = PropertySignal
