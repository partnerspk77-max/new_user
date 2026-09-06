"""
Record normalizer and validator for Miami-Dade County Building Permits.
Transforms raw ArcGIS feature payloads into NormalizedPermit domain entities.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from src.client.exceptions import ValidationError
from src.logger import logger
from src.models.permit import NormalizedPermit, RawRecord

SOURCE_NAME = "miami_dade_arcgis"


def parse_arcgis_timestamp(value: Any) -> Optional[str]:
    """
    Safely parses an ArcGIS date value (epoch milliseconds or ISO string)
    into a timezone-aware ISO 8601 UTC string (YYYY-MM-DDTHH:MM:SSZ).
    Returns None if value is null or unparseable; never invents fake dates.
    """
    if value is None or value == "":
        return None

    # Handle integer / float epoch milliseconds (standard ArcGIS REST date representation)
    if isinstance(value, (int, float)):
        try:
            # ArcGIS timestamps are in milliseconds
            seconds = value / 1000.0
            # Sanity check range: year 1900 to 2100
            if seconds < -2208988800 or seconds > 4102444800:
                logger.warning(f"Timestamp value {value} out of expected sane range. Discarding.")
                return None
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError) as e:
            logger.warning(f"Failed to convert epoch ms timestamp {value}: {e}")
            return None

    # Handle string representations
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        # Check if numeric string
        if cleaned.lstrip("-").isdigit():
            return parse_arcgis_timestamp(int(cleaned))
        
        # Try ISO parsing
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(cleaned, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue

        logger.warning(f"Unparseable date string: '{value}'")
        return None

    return None


def clean_string(value: Any) -> Optional[str]:
    """
    Normalizes string by stripping leading/trailing whitespace.
    Preserves exact casing and characters. Empty strings become None.
    """
    if value is None:
        return None
    val_str = str(value).strip()
    return val_str if val_str else None


def clean_identifier(value: Any) -> Optional[str]:
    """
    Normalizes string identifiers (ID, PROCNUM, FOLIO, CONTRNUM)
    without losing precision (e.g. leading zeros) or converting to float.
    """
    if value is None:
        return None
    val_str = str(value).strip()
    # Avoid scientific notation strings or float formats like '12345.0'
    if val_str.endswith(".0"):
        val_str = val_str[:-2]
    return val_str if val_str else None


def extract_coordinates(geometry: Optional[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """
    Extracts (latitude, longitude) from ArcGIS point geometry in WGS84 (outSR=4326).
    ArcGIS point geometry format: {'x': longitude, 'y': latitude}.
    Returns (None, None) if missing or out of valid geographic bounds.
    """
    if not geometry or not isinstance(geometry, dict):
        return None, None

    x = geometry.get("x")
    y = geometry.get("y")

    if x is None or y is None:
        return None, None

    try:
        lon = float(x)
        lat = float(y)

        # Validate WGS84 geographic coordinate bounds
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            return lat, lon
        else:
            logger.warning(f"Coordinates out of WGS84 bounds: lat={lat}, lon={lon}")
            return None, None
    except (ValueError, TypeError):
        logger.warning(f"Failed to parse coordinates: x={x}, y={y}")
        return None, None


class PermitNormalizer:
    """Normalizes raw ArcGIS features into domain models."""

    @staticmethod
    def normalize_feature(
        feature: Dict[str, Any],
        fetched_at: Optional[str] = None,
    ) -> Tuple[RawRecord, NormalizedPermit]:
        """
        Takes an individual ArcGIS feature (containing 'attributes' and optional 'geometry')
        and returns a pair: (RawRecord, NormalizedPermit).
        Raises ValidationError if required stable identifiers are missing.
        """
        attributes = feature.get("attributes") or {}
        geometry = feature.get("geometry")

        now_utc = fetched_at or datetime.now(timezone.utc).isoformat()

        # Extract primary identifiers
        raw_object_id = attributes.get("OBJECTID")
        source_object_id: Optional[int] = None
        if raw_object_id is not None:
            try:
                source_object_id = int(raw_object_id)
            except (ValueError, TypeError):
                pass

        global_id = clean_string(attributes.get("GlobalID"))
        permit_number = clean_identifier(attributes.get("ID"))
        process_number = clean_identifier(attributes.get("PROCNUM"))
        folio = clean_identifier(attributes.get("FOLIO"))

        # Validation: At least one stable source identifier must exist
        if source_object_id is None and not permit_number and not process_number and not global_id:
            raise ValidationError(
                f"Record has no stable source identifier (OBJECTID, ID, PROCNUM, or GlobalID): {attributes}"
            )

        # Stable internal unique ID
        if source_object_id is not None:
            internal_id = f"{SOURCE_NAME}:{source_object_id}"
        elif permit_number and process_number:
            internal_id = f"{SOURCE_NAME}:{permit_number}:{process_number}"
        elif global_id:
            internal_id = f"{SOURCE_NAME}:{global_id}"
        else:
            internal_id = f"{SOURCE_NAME}:{permit_number or process_number}"

        # 1. Create RawRecord (preserve complete unmodified feature response)
        raw_record = RawRecord(
            source=SOURCE_NAME,
            source_object_id=source_object_id or 0,
            global_id=global_id,
            fetched_at=now_utc,
            raw_payload=feature,
        )

        # 2. Extract Geometry (WGS84 lat/lon)
        lat, lon = extract_coordinates(geometry)

        # 3. Create NormalizedPermit
        normalized = NormalizedPermit(
            id=internal_id,
            source=SOURCE_NAME,
            source_object_id=source_object_id or 0,
            global_id=global_id,
            folio=folio,
            permit_number=permit_number,
            process_number=process_number,
            address=clean_string(attributes.get("ADDRESS")),
            unit=clean_string(attributes.get("UNIT")),
            is_condo=clean_string(attributes.get("ISCONDO")),
            permit_type=clean_string(attributes.get("TYPE")),
            # Categories 1-10 & Descriptions 1-10
            category_1=clean_string(attributes.get("CAT1")),
            description_1=clean_string(attributes.get("DESC1")),
            category_2=clean_string(attributes.get("CAT2")),
            description_2=clean_string(attributes.get("DESC2")),
            category_3=clean_string(attributes.get("CAT3")),
            description_3=clean_string(attributes.get("DESC3")),
            category_4=clean_string(attributes.get("CAT4")),
            description_4=clean_string(attributes.get("DESC4")),
            category_5=clean_string(attributes.get("CAT5")),
            description_5=clean_string(attributes.get("DESC5")),
            category_6=clean_string(attributes.get("CAT6")),
            description_6=clean_string(attributes.get("DESC6")),
            category_7=clean_string(attributes.get("CAT7")),
            description_7=clean_string(attributes.get("DESC7")),
            category_8=clean_string(attributes.get("CAT8")),
            description_8=clean_string(attributes.get("DESC8")),
            category_9=clean_string(attributes.get("CAT9")),
            description_9=clean_string(attributes.get("DESC9")),
            category_10=clean_string(attributes.get("CAT10")),
            description_10=clean_string(attributes.get("DESC10")),
            # Timestamps
            issued_at=parse_arcgis_timestamp(attributes.get("ISSUDATE")),
            last_inspection_at=parse_arcgis_timestamp(attributes.get("LSTINSDT")),
            renewal_at=parse_arcgis_timestamp(attributes.get("RENDATE")),
            completion_at=parse_arcgis_timestamp(attributes.get("BLDCMPDT")),
            last_approval_at=parse_arcgis_timestamp(attributes.get("LSTAPPRDT")),
            # Metadata & Classifiers
            residential_commercial=clean_string(attributes.get("RESCOMM")),
            proposed_use=clean_string(attributes.get("PROPUSE")),
            application_type=clean_string(attributes.get("APPTYPE")),
            comment=clean_string(attributes.get("FFRMLINE")),
            master_permit_number=clean_identifier(attributes.get("MPRMTNUM")),
            contractor_number=clean_identifier(attributes.get("CONTRNUM")),
            contractor_name=clean_string(attributes.get("CONTRNAME")),
            status=clean_string(attributes.get("BPSTATUS")),
            # Geometry
            latitude=lat,
            longitude=lon,
            source_fetched_at=now_utc,
            created_at=now_utc,
            updated_at=now_utc,
        )

        return raw_record, normalized
