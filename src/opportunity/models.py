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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["opportunity_id"] = self.opportunity_id
        d["opportunity_type"] = self.opportunity_type
        d["priority_score"] = self.priority_score
        return d


# Backward compatibility alias
CommercialOpportunity = PropertySignal
