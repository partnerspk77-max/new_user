"""
Integration and pipeline tests for BackfillService and SyncService.
Tests pagination across multiple pages, incremental synchronization with overlap,
infinite loop protection, and metrics collection.
"""

from unittest.mock import MagicMock
import pytest

from src.client.arcgis_client import ArcGISClient
from src.pipeline.backfill import BackfillService
from src.pipeline.sync import SyncService
from src.pipeline.normalizer import PermitNormalizer


def _create_mock_feature(object_id: int, issue_timestamp: int, status: str = "A"):
    return {
        "attributes": {
            "OBJECTID": object_id,
            "FOLIO": f"30-4015-{object_id:04d}",
            "ID": f"PERM-{object_id}",
            "PROCNUM": f"PROC-{object_id}",
            "ADDRESS": f"{object_id} BISCAYNE BLVD",
            "UNIT": None,
            "ISCONDO": "N",
            "TYPE": "BUILDING",
            "CAT1": "01",
            "DESC1": "ROOFING - SHINGLE",
            "ISSUDATE": issue_timestamp,
            "LSTINSDT": None,
            "RENDATE": None,
            "BLDCMPDT": None,
            "RESCOMM": "RESIDENTIAL",
            "PROPUSE": "SINGLE FAMILY",
            "APPTYPE": "NEW",
            "FFRMLINE": "ROOF REPLACEMENT",
            "MPRMTNUM": None,
            "LSTAPPRDT": None,
            "CONTRNUM": "CGC123",
            "CONTRNAME": "ACE ROOFING",
            "BPSTATUS": status,
            "GlobalID": f"{{GUID-{object_id}}}",
        },
        "geometry": {"x": -80.19, "y": 25.76},
    }


class TestBackfillService:
    def test_multi_page_backfill_pagination(self, raw_store, permit_store, state_store):
        """Simulates > 1000 records across 2 pages (page 1: 1000 items, page 2: 250 items)."""
        mock_client = MagicMock(spec=ArcGISClient)
        mock_client.page_size = 1000

        # Page 1: 1000 records
        page1_features = [_create_mock_feature(i, 1704067200000 + (i * 1000)) for i in range(1, 1001)]
        resp_page1 = {
            "features": page1_features,
            "exceededTransferLimit": True,
        }

        # Page 2: 250 records
        page2_features = [_create_mock_feature(i, 1705067200000 + (i * 1000)) for i in range(1001, 1251)]
        resp_page2 = {
            "features": page2_features,
            "exceededTransferLimit": False,
        }

        # Page 3: Empty (if queried)
        resp_page3 = {"features": []}

        mock_client.query_records.side_effect = [resp_page1, resp_page2, resp_page3]

        service = BackfillService(mock_client, raw_store, permit_store, state_store)
        metrics = service.run(days=30)

        assert metrics.total_records == 1250
        assert metrics.new_records == 1250
        assert metrics.duplicate_records == 0
        assert metrics.invalid_records == 0
        assert permit_store.count() == 1250
        assert raw_store.count() == 1250

        # Verify state store checkpoint
        state = state_store.get_state("miami_dade_arcgis")
        assert state is not None
        assert state["status"] == "completed"
        assert state["total_ingested"] == 1250

    def test_infinite_loop_protection(self, raw_store, permit_store, state_store):
        """Ensures backfill stops if the server repeatedly returns the same single record."""
        mock_client = MagicMock(spec=ArcGISClient)
        mock_client.page_size = 1

        stuck_feature = _create_mock_feature(999, 1704067200000)
        stuck_response = {"features": [stuck_feature], "exceededTransferLimit": True}

        # Repeated responses of the exact same record
        mock_client.query_records.side_effect = [stuck_response, stuck_response, stuck_response]

        service = BackfillService(mock_client, raw_store, permit_store, state_store)
        metrics = service.run(days=30)

        # Loop protection halts execution cleanly
        assert metrics.total_records == 1
        assert permit_store.count() == 1


class TestSyncService:
    def test_incremental_sync_with_overlap_and_updates(self, raw_store, permit_store, state_store):
        """
        1. Pre-ingest record 100 with status 'A'.
        2. Sync runs with overlap: returns record 100 with updated status 'F', and brand new record 101.
        3. Verify record 100 is updated (not duplicated) and record 101 is inserted.
        """
        # Step 1: Pre-populate record 100 (issue date: 2024-01-01 00:00:00 UTC = 1704067200000)
        rec100_v1 = _create_mock_feature(100, 1704067200000, status="A")
        raw1, norm1 = PermitNormalizer.normalize_feature(rec100_v1)
        raw_store.insert_batch([raw1])
        permit_store.upsert_batch([norm1])
        state_store.update_checkpoint(
            job_id="miami_dade_arcgis",
            last_processed_date="2024-01-01T00:00:00Z",
            total_ingested=1,
            status="completed",
        )

        assert permit_store.count() == 1
        assert permit_store.get_by_object_id("miami_dade_arcgis", 100)["status"] == "A"

        # Step 2: Set up mock client for incremental sync
        # Record 100 updated to 'F', and Record 101 is new
        rec100_v2 = _create_mock_feature(100, 1704067200000, status="F")
        rec101 = _create_mock_feature(101, 1704070800000, status="A")

        mock_client = MagicMock(spec=ArcGISClient)
        mock_client.page_size = 1000
        mock_client.query_records.side_effect = [
            {"features": [rec100_v2, rec101], "exceededTransferLimit": False},
        ]

        sync_service = SyncService(mock_client, raw_store, permit_store, state_store)
        metrics = sync_service.run(overlap_minutes=60)

        # Step 3: Verify results
        assert metrics.total_records == 2
        assert metrics.new_records == 1      # Record 101
        assert metrics.updated_records == 1  # Record 100 updated
        assert metrics.duplicate_records == 0

        # Database must have exactly 2 records total (no duplicates)
        assert permit_store.count() == 2

        # Record 100 must now have status 'F'
        updated_100 = permit_store.get_by_object_id("miami_dade_arcgis", 100)
        assert updated_100["status"] == "F"

        # Raw store must preserve both payloads
        assert raw_store.count() == 3  # Initial + 2 sync records
