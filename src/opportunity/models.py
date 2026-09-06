"""
Domain models for Property Opportunity Engine.
Supports dual-track commercial intelligence:
- Track A: Project Intelligence (Material Suppliers, Distributors, Equipment Rental, Canvassers)
- Track B: Pre-Permit & Unassigned Opportunities (Roofing Contractors seeking uncontracted leads)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class PropertyTimeline:
    """Aggregated multi-trade timeline for a single property parcel (Folio)."""
    folio: str
    address: Optional[str] = None
    total_permits: int = 0
    trades: List[str] = field(default_factory=list)
    roof_permits_count: int = 0
    has_active_roof_permit: bool = False
    last_roof_permit_date: Optional[str] = None
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
class CommercialOpportunity:
    """Actionable commercial opportunity for roofing contractors, suppliers, or canvassers."""
    opportunity_id: str
    folio: str
    address: Optional[str]
    opportunity_type: str
    target_audience: str  # 'ROOFING_CONTRACTOR' or 'SUPPLIER_DISTRIBUTOR'
    priority_score: float  # 0.0 to 100.0
    freshness_tier: str  # 'NEW (0-24h)', 'FRESH (1-3d)', 'RECENT (4-7d)', 'STALE (8-30d)', 'HISTORICAL (30d+)'
    age_hours: Optional[float]
    age_days: Optional[float]
    contractor_present: bool
    contractor_name: Optional[str]
    trigger_trade: str
    trigger_event: str
    recommended_action: str
    residential_commercial: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
