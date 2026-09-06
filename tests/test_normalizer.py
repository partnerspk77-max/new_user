"""
Unit tests for data normalization, date conversion, and geometry extraction.
"""

import pytest
from src.client.exceptions import ValidationError
from src.pipeline.normalizer import (
    PermitNormalizer,
    clean_identifier,
    clean_string,
    extract_coordinates,
    parse_arcgis_timestamp,
)


class TestTimestampParsing:
    def test_epoch_milliseconds_conversion(self):
        # 1704067200000 = 2024-01-01 00:00:00 UTC
        result = parse_arcgis_timestamp(1704067200000)
        assert result == "2024-01-01T00:00:00Z"

    def test_string_numeric_conversion(self):
        result = parse_arcgis_timestamp("1704067200000")
        assert result == "2024-01-01T00:00:00Z"

    def test_iso_string_conversion(self):
        result = parse_arcgis_timestamp("2024-01-01 12:30:00")
        assert result == "2024-01-01T12:30:00Z"

    def test_null_and_empty_values(self):
        assert parse_arcgis_timestamp(None) is None
        assert parse_arcgis_timestamp("") is None
        assert parse_arcgis_timestamp("   ") is None

    def test_invalid_dates_return_none_without_inventing_dates(self):
        assert parse_arcgis_timestamp("invalid-date-format") is None
        # Extreme out-of-range epoch timestamp (e.g. year 30000)
        assert parse_arcgis_timestamp(9999999999999999) is None


class TestGeometryExtraction:
    def test_valid_wgs84_point(self):
        geom = {"x": -80.1918, "y": 25.7617}
        lat, lon = extract_coordinates(geom)
        assert lat == pytest.approx(25.7617)
        assert lon == pytest.approx(-80.1918)

    def test_missing_or_null_geometry(self):
        assert extract_coordinates(None) == (None, None)
        assert extract_coordinates({}) == (None, None)
        assert extract_coordinates({"x": None, "y": 25.0}) == (None, None)

    def test_out_of_bounds_coordinates(self):
        # Latitude out of bounds (> 90)
        assert extract_coordinates({"x": -80.0, "y": 150.0}) == (None, None)
        # Longitude out of bounds (< -180)
        assert extract_coordinates({"x": -200.0, "y": 25.0}) == (None, None)


class TestStringAndIdentifierCleaning:
    def test_clean_string(self):
        assert clean_string("  Miami Beach  ") == "Miami Beach"
        assert clean_string("") is None
        assert clean_string(None) is None

    def test_clean_identifier_preserves_leading_zeros(self):
        assert clean_identifier("0076543") == "0076543"
        assert clean_identifier(" 00123 ") == "00123"

    def test_clean_identifier_avoids_float_corruption(self):
        assert clean_identifier("123456.0") == "123456"


class TestFeatureNormalization:
    def test_complete_feature_normalization(self, sample_arcgis_feature):
        raw, norm = PermitNormalizer.normalize_feature(sample_arcgis_feature)

        # Verify RawRecord
        assert raw.source == "miami_dade_arcgis"
        assert raw.source_object_id == 1001
        assert raw.global_id == "{ABC-123-XYZ}"
        assert raw.raw_payload == sample_arcgis_feature

        # Verify NormalizedPermit
        assert norm.id == "miami_dade_arcgis:1001"
        assert norm.permit_number == "20240101001"
        assert norm.process_number == "M2024001"
        assert norm.folio == "30-4015-001-0020"
        assert norm.address == "123 BISCAYNE BLVD"
        assert norm.unit == "STE 400"
        assert norm.permit_type == "BUILDING"
        assert norm.category_1 == "01"
        assert norm.description_1 == "ROOFING - TILE"
        assert norm.category_2 == "02"
        assert norm.description_2 == "ELECTRICAL"
        assert norm.issued_at == "2024-01-01T00:00:00Z"
        assert norm.last_inspection_at == "2024-01-08T00:00:00Z"
        assert norm.contractor_name == "SOUTH FLORIDA ROOFING INC"
        assert norm.contractor_number == "CGC1500000"
        assert norm.status == "A"
        assert norm.latitude == pytest.approx(25.7617)
        assert norm.longitude == pytest.approx(-80.1918)

    def test_missing_identity_raises_validation_error(self):
        bad_feature = {
            "attributes": {
                "OBJECTID": None,
                "ID": None,
                "PROCNUM": None,
                "GlobalID": None,
                "ADDRESS": "123 Ghost Lane",
            },
            "geometry": None,
        }
        with pytest.raises(ValidationError):
            PermitNormalizer.normalize_feature(bad_feature)

    def test_null_geometry_handled_gracefully(self, sample_arcgis_feature):
        sample_arcgis_feature["geometry"] = None
        raw, norm = PermitNormalizer.normalize_feature(sample_arcgis_feature)
        assert norm.latitude is None
        assert norm.longitude is None
        assert norm.source_object_id == 1001
