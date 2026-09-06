"""
Data models for Raw and Normalized Permit Records, and Ingestion Metrics.
Follows clean architecture and strict typing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class RawRecord:
    """Represents an un-mutated API response record for auditability & reprocessing."""
    source: str
    source_object_id: int
    global_id: Optional[str]
    fetched_at: str  # ISO 8601 UTC
    raw_payload: Dict[str, Any]

    def to_db_row(self) -> tuple:
        return (
            self.source,
            self.source_object_id,
            self.global_id,
            self.fetched_at,
            json.dumps(self.raw_payload, sort_keys=True),
        )


@dataclass
class NormalizedPermit:
    """
    Internal normalized permit representation.
    Preserves original source identifiers and classifier categories/descriptions.
    """
    id: str  # Deterministic identifier: e.g. f"{source}:{source_object_id}"
    source: str  # "miami_dade_arcgis"
    source_object_id: int
    global_id: Optional[str]
    folio: Optional[str]
    permit_number: Optional[str]  # Source: ID
    process_number: Optional[str]  # Source: PROCNUM
    address: Optional[str]
    unit: Optional[str]
    is_condo: Optional[str]
    permit_type: Optional[str]  # Source: TYPE

    # Preserved categories & descriptions (CAT1-CAT10, DESC1-DESC10)
    category_1: Optional[str] = None
    description_1: Optional[str] = None
    category_2: Optional[str] = None
    description_2: Optional[str] = None
    category_3: Optional[str] = None
    description_3: Optional[str] = None
    category_4: Optional[str] = None
    description_4: Optional[str] = None
    category_5: Optional[str] = None
    description_5: Optional[str] = None
    category_6: Optional[str] = None
    description_6: Optional[str] = None
    category_7: Optional[str] = None
    description_7: Optional[str] = None
    category_8: Optional[str] = None
    description_8: Optional[str] = None
    category_9: Optional[str] = None
    description_9: Optional[str] = None
    category_10: Optional[str] = None
    description_10: Optional[str] = None

    # Timestamps (ISO 8601 UTC)
    issued_at: Optional[str] = None  # Source: ISSUDATE
    last_inspection_at: Optional[str] = None  # Source: LSTINSDT
    renewal_at: Optional[str] = None  # Source: RENDATE
    completion_at: Optional[str] = None  # Source: BLDCMPDT
    last_approval_at: Optional[str] = None  # Source: LSTAPPRDT

    # Classifiers & Contractor Details
    residential_commercial: Optional[str] = None  # Source: RESCOMM
    proposed_use: Optional[str] = None  # Source: PROPUSE
    application_type: Optional[str] = None  # Source: APPTYPE
    comment: Optional[str] = None  # Source: FFRMLINE
    master_permit_number: Optional[str] = None  # Source: MPRMTNUM
    contractor_number: Optional[str] = None  # Source: CONTRNUM
    contractor_name: Optional[str] = None  # Source: CONTRNAME
    status: Optional[str] = None  # Source: BPSTATUS (A=Active, E=Expired, F=Finalized)

    # Coordinates in WGS84
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Tracking
    source_fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IngestionMetrics:
    """Data quality metrics collected during pipeline execution."""
    total_records: int = 0
    new_records: int = 0
    updated_records: int = 0
    duplicate_records: int = 0
    invalid_records: int = 0
    records_with_folio: int = 0
    records_with_contractor: int = 0
    records_with_geometry: int = 0
    records_with_issue_date: int = 0
    min_issue_date: Optional[str] = None
    max_issue_date: Optional[str] = None
    duration_seconds: float = 0.0

    def update_issue_date(self, issue_date: Optional[str]) -> None:
        if not issue_date:
            return
        self.records_with_issue_date += 1
        if self.min_issue_date is None or issue_date < self.min_issue_date:
            self.min_issue_date = issue_date
        if self.max_issue_date is None or issue_date > self.max_issue_date:
            self.max_issue_date = issue_date

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
