"""
Data models for Miami-Dade Property Appraiser (PAPA) parcel enrichment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class PropertyParcel:
    """Represents property appraisal and physical structural data for a parcel."""

    folio: str  # 13-digit normalized string without hyphens
    year_built: Optional[int] = None
    building_actual_area: Optional[float] = None
    building_heated_area: Optional[float] = None
    bedroom_count: Optional[int] = None
    bathroom_count: Optional[float] = None
    dor_code: Optional[str] = None
    dor_desc: Optional[str] = None
    owner_name: Optional[str] = None
    site_address: Optional[str] = None
    site_zip: Optional[str] = None
    mailing_address: Optional[str] = None
    assessed_value: Optional[float] = None
    is_vacant: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def building_age(self) -> Optional[int]:
        """Calculates building age in years relative to current calendar year."""
        if self.year_built is not None and 1800 <= self.year_built <= datetime.now(timezone.utc).year:
            return datetime.now(timezone.utc).year - self.year_built
        return None

    @property
    def estimated_roof_squares(self) -> Optional[float]:
        """
        Estimates total roofing squares (1 square = 100 sq ft).
        Uses actual building footprint area if available.
        """
        area = self.building_actual_area or self.building_heated_area
        if area and area > 0:
            # 1 roofing square = 100 sq ft; round to 1 decimal place
            return round(area / 100.0, 1)
        return None

    @classmethod
    def from_arcgis_attributes(cls, attrs: Dict[str, Any]) -> PropertyParcel:
        """Parses raw Esri ArcGIS PaGISView feature attributes into a clean domain model."""
        raw_folio = str(attrs.get("FOLIO") or "").replace("-", "").strip()

        # Year built: 0 or None indicates unrecorded or vacant land
        raw_yb = attrs.get("YEAR_BUILT")
        year_built: Optional[int] = None
        if raw_yb is not None:
            try:
                yb_int = int(raw_yb)
                if 1800 <= yb_int <= datetime.now(timezone.utc).year:
                    year_built = yb_int
            except (ValueError, TypeError):
                pass

        # Building actual area
        raw_area = attrs.get("BUILDING_ACTUAL_AREA")
        actual_area: Optional[float] = None
        if raw_area is not None:
            try:
                val = float(raw_area)
                if val > 0:
                    actual_area = val
            except (ValueError, TypeError):
                pass

        # Building heated / living area
        raw_heated = attrs.get("BUILDING_HEATED_AREA")
        heated_area: Optional[float] = None
        if raw_heated is not None:
            try:
                val = float(raw_heated)
                if val > 0:
                    heated_area = val
            except (ValueError, TypeError):
                pass

        # Assessed value
        raw_val = attrs.get("ASSESSED_VAL_CUR")
        assessed_val: Optional[float] = None
        if raw_val is not None:
            try:
                assessed_val = float(raw_val)
            except (ValueError, TypeError):
                pass

        # Owner name
        owner = attrs.get("TRUE_OWNER1")
        if owner:
            owner = str(owner).strip()
            if not owner or owner.upper() in ("NONE", "NULL", "UNKNOWN"):
                owner = None

        # Site Address & Zip
        site_addr = attrs.get("TRUE_SITE_ADDR")
        if site_addr:
            site_addr = str(site_addr).strip()
            if not site_addr or site_addr.upper() in ("NONE", "NULL"):
                site_addr = None

        site_zip = attrs.get("TRUE_SITE_ZIP_CODE")
        if site_zip:
            site_zip = str(site_zip).strip()
            if not site_zip or site_zip.upper() in ("NONE", "NULL"):
                site_zip = None

        # Mailing Address
        mailing_parts = [
            str(attrs.get(k) or "").strip()
            for k in ("TRUE_MAILING_ADDR1", "TRUE_MAILING_ADDR2", "TRUE_MAILING_ZIP_CODE")
            if attrs.get(k)
        ]
        mailing_address = ", ".join(mailing_parts) if mailing_parts else None

        # DOR classification
        dor_code = attrs.get("DOR_CODE_CUR")
        dor_desc = attrs.get("DOR_DESC")
        if dor_desc:
            dor_desc = str(dor_desc).strip()

        # Vacancy or Reference parcel flag
        is_vacant = False
        if dor_desc:
            desc_upper = dor_desc.upper()
            if "VACANT" in desc_upper or "REFERENCE" in desc_upper or "COMMON AREA" in desc_upper:
                is_vacant = True
        if year_built is None and (actual_area is None or actual_area == 0):
            is_vacant = True

        # Bedrooms & Bathrooms
        bed_count = None
        if attrs.get("BEDROOM_COUNT") is not None:
            try:
                bed_count = int(attrs["BEDROOM_COUNT"])
            except (ValueError, TypeError):
                pass

        bath_count = None
        if attrs.get("BATHROOM_COUNT") is not None:
            try:
                bath_count = float(attrs["BATHROOM_COUNT"])
            except (ValueError, TypeError):
                pass

        return cls(
            folio=raw_folio,
            year_built=year_built,
            building_actual_area=actual_area,
            building_heated_area=heated_area,
            bedroom_count=bed_count,
            bathroom_count=bath_count,
            dor_code=str(dor_code).strip() if dor_code else None,
            dor_desc=dor_desc,
            owner_name=owner,
            site_address=site_addr,
            site_zip=site_zip,
            mailing_address=mailing_address,
            assessed_value=assessed_val,
            is_vacant=is_vacant,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes parcel to dictionary."""
        data = asdict(self)
        data["building_age"] = self.building_age
        data["estimated_roof_squares"] = self.estimated_roof_squares
        return data
