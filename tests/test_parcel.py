"""
Unit and integration tests for Miami-Dade Property Appraiser (PAPA) parcel enrichment.
Tests:
- PropertyParcel domain modeling and dynamic age calculation
- ParcelClient REST API batch querying and error resilience
- ParcelStorage persistence, upserts, batch lookups, and statistics
"""

import pytest
from unittest.mock import MagicMock, patch

from src.client.parcel_client import ParcelClient
from src.enrichment.models import PropertyParcel
from src.enrichment.storage import ParcelStorage
from src.storage.database import Database


class TestPropertyParcelModel:
    def test_from_arcgis_attributes_clean(self):
        raw = {
            "FOLIO": "30-4015-001-0010",
            "YEAR_BUILT": 1952,
            "BUILDING_ACTUAL_AREA": 1850.0,
            "BUILDING_HEATED_AREA": 1600.0,
            "BEDROOM_COUNT": 3,
            "BATHROOM_COUNT": 2.0,
            "DOR_CODE_CUR": "01",
            "DOR_DESC": "RESIDENTIAL - SINGLE FAMILY : 1 UNIT",
            "TRUE_OWNER1": "JOHN DOE & JANE DOE",
            "TRUE_SITE_ADDR": "1234 OCEAN DR",
            "TRUE_SITE_ZIP_CODE": "33139",
            "TRUE_MAILING_ADDR1": "1234 OCEAN DR",
            "TRUE_MAILING_ZIP_CODE": "33139",
            "ASSESSED_VAL_CUR": 450000.0,
        }

        p = PropertyParcel.from_arcgis_attributes(raw)
        assert p.folio == "3040150010010"
        assert p.year_built == 1952
        assert p.building_age is not None and p.building_age >= 70
        assert p.building_actual_area == 1850.0
        assert p.estimated_roof_squares == 18.5
        assert p.owner_name == "JOHN DOE & JANE DOE"
        assert p.dor_desc == "RESIDENTIAL - SINGLE FAMILY : 1 UNIT"
        assert p.is_vacant is False

    def test_building_age_calculation(self):
        # Normal construction
        p1 = PropertyParcel(folio="01", year_built=1970)
        assert p1.building_age is not None and p1.building_age > 50

        # Unrecorded / Vacant (0)
        p2 = PropertyParcel(folio="02", year_built=0)
        assert p2.building_age is None

        # None
        p3 = PropertyParcel(folio="03", year_built=None)
        assert p3.building_age is None

        # Impossibly old
        p4 = PropertyParcel(folio="04", year_built=1700)
        assert p4.building_age is None

    def test_vacant_and_reference_parcels(self):
        raw_vacant = {
            "FOLIO": "0102050201030",
            "YEAR_BUILT": 0,
            "BUILDING_ACTUAL_AREA": 0,
            "DOR_DESC": "VACANT LAND - COMMERCIAL : EXTRA FEA",
            "TRUE_OWNER1": "LAND HOLDINGS LLC",
        }
        p = PropertyParcel.from_arcgis_attributes(raw_vacant)
        assert p.is_vacant is True
        assert p.building_age is None
        assert p.estimated_roof_squares is None


class TestParcelClient:
    def test_fetch_parcels_batch_mock(self):
        mock_response = {
            "features": [
                {
                    "attributes": {
                        "FOLIO": "3010010000010",
                        "YEAR_BUILT": 1965,
                        "BUILDING_ACTUAL_AREA": 2400.0,
                        "DOR_DESC": "RESIDENTIAL - SINGLE FAMILY",
                        "TRUE_OWNER1": "ALICE SMITH",
                        "TRUE_SITE_ADDR": "500 BRICKELL AVE",
                        "ASSESSED_VAL_CUR": 750000.0,
                    }
                }
            ]
        }

        mock_session = MagicMock()
        mock_resp_obj = MagicMock()
        mock_resp_obj.json.return_value = mock_response
        mock_resp_obj.raise_for_status.return_value = None
        mock_session.post.return_value = mock_resp_obj

        client = ParcelClient(session=mock_session)
        parcels = client.fetch_parcels_batch(["30-1001-000-0010"])

        assert len(parcels) == 1
        assert parcels[0].folio == "3010010000010"
        assert parcels[0].year_built == 1965
        assert parcels[0].owner_name == "ALICE SMITH"
        assert parcels[0].estimated_roof_squares == 24.0

    def test_fetch_parcels_batch_error_handling(self):
        mock_session = MagicMock()
        mock_session.post.side_effect = Exception("ArcGIS connection timeout")

        client = ParcelClient(session=mock_session)
        parcels = client.fetch_parcels_batch(["30-1001-000-0010"])
        assert parcels == []

    def test_fetch_all_parcels_chunking(self):
        client = ParcelClient(batch_size=2, request_delay=0.0)
        sample_folios = ["F01", "F02", "F03", "F04", "F05"]

        with patch.object(client, "fetch_parcels_batch") as mock_batch:
            mock_batch.side_effect = [
                [PropertyParcel(folio="F01"), PropertyParcel(folio="F02")],
                [PropertyParcel(folio="F03"), PropertyParcel(folio="F04")],
                [PropertyParcel(folio="F05")],
            ]

            results = client.fetch_all_parcels(sample_folios)
            assert len(results) == 5
            assert mock_batch.call_count == 3


class TestParcelStorage:
    @pytest.fixture
    def test_db(self):
        db = Database("sqlite:///:memory:")
        db.initialize_schema()
        return db

    def test_upsert_and_retrieve_parcels(self, test_db):
        storage = ParcelStorage(test_db)

        parcels = [
            PropertyParcel(
                folio="3010010000010",
                year_built=1958,
                building_actual_area=2200.0,
                dor_desc="SINGLE FAMILY",
                owner_name="ROBERTO CARLOS",
                assessed_value=520000.0,
            ),
            PropertyParcel(
                folio="3010010000020",
                year_built=2021,
                building_actual_area=3500.0,
                dor_desc="COMMERCIAL RETAIL",
                owner_name="RETAIL PROPERTIES LLC",
                assessed_value=1200000.0,
            ),
        ]

        saved = storage.upsert_parcels(parcels)
        assert saved == 2

        p1 = storage.get_parcel("3010010000010")
        assert p1 is not None
        assert p1.year_built == 1958
        assert p1.building_age is not None and p1.building_age >= 65
        assert p1.owner_name == "ROBERTO CARLOS"

        # Lookup with hyphens
        p1_hyphen = storage.get_parcel("30-1001-000-0010")
        assert p1_hyphen is not None
        assert p1_hyphen.folio == "3010010000010"

    def test_get_parcels_map_and_unenriched(self, test_db):
        storage = ParcelStorage(test_db)

        p = PropertyParcel(folio="3010010000010", year_built=1980)
        storage.upsert_parcels([p])

        pmap = storage.get_parcels_map(["30-1001-000-0010", "30-1001-000-0099"])
        assert "3010010000010" in pmap
        assert "3010010000099" not in pmap

        unenriched = storage.get_unenriched_folios(["30-1001-000-0010", "30-1001-000-0099"])
        assert unenriched == ["3010010000099"]

    def test_stats_calculation(self, test_db):
        storage = ParcelStorage(test_db)
        parcels = [
            PropertyParcel(folio="F1", year_built=1950, building_actual_area=1500, owner_name="OWNER A"),
            PropertyParcel(folio="F2", year_built=2000, building_actual_area=2500, owner_name="OWNER B"),
            PropertyParcel(folio="F3", year_built=0, building_actual_area=0, is_vacant=True),
        ]
        storage.upsert_parcels(parcels)

        stats = storage.get_stats()
        assert stats["total_parcels"] == 3
        assert stats["parcels_with_year_built"] == 2
        assert stats["parcels_with_area"] == 2
        assert stats["parcels_with_owner"] == 2
        assert stats["avg_year_built"] == 1975.0
