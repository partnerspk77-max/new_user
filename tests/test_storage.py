"""
Unit tests for storage engines: RawStore, PermitStore, and StateStore.
Tests deterministic upserts, deduplication, update preservation, and checkpointing.
"""

from datetime import datetime, timezone
from pathlib import Path
from src.pipeline.normalizer import PermitNormalizer


class TestRawStore:
    def test_raw_record_insertion_and_retrieval(self, raw_store, sample_arcgis_feature):
        raw, _ = PermitNormalizer.normalize_feature(sample_arcgis_feature)
        inserted = raw_store.insert_batch([raw])
        assert inserted == 1
        assert raw_store.count() == 1

        retrieved = raw_store.get_by_object_id("miami_dade_arcgis", 1001)
        assert retrieved is not None
        assert retrieved["source_object_id"] == 1001
        assert retrieved["raw_payload"]["attributes"]["ID"] == "20240101001"


class TestPermitStoreDeduplicationAndUpdates:
    def test_initial_insert(self, permit_store, sample_arcgis_feature):
        _, norm = PermitNormalizer.normalize_feature(sample_arcgis_feature)
        new_cnt, upd_cnt, dup_cnt = permit_store.upsert_batch([norm])

        assert new_cnt == 1
        assert upd_cnt == 0
        assert dup_cnt == 0
        assert permit_store.count() == 1

    def test_exact_duplicate_does_not_create_new_row(self, permit_store, sample_arcgis_feature):
        _, norm = PermitNormalizer.normalize_feature(sample_arcgis_feature)
        permit_store.upsert_batch([norm])

        # Re-insert the exact same permit
        new_cnt, upd_cnt, dup_cnt = permit_store.upsert_batch([norm])
        assert new_cnt == 0
        assert upd_cnt == 0
        assert dup_cnt == 1
        assert permit_store.count() == 1  # No duplicate rows!

    def test_update_existing_permit(self, permit_store, sample_arcgis_feature):
        _, norm1 = PermitNormalizer.normalize_feature(sample_arcgis_feature)
        permit_store.upsert_batch([norm1])

        # Modify fields: new status, new contractor, new completion date
        feature_updated = dict(sample_arcgis_feature)
        feature_updated["attributes"] = dict(sample_arcgis_feature["attributes"])
        feature_updated["attributes"]["BPSTATUS"] = "F"  # Finalized
        feature_updated["attributes"]["CONTRNAME"] = "SUPERIOR ROOFING LLC"
        feature_updated["attributes"]["BLDCMPDT"] = 1705000000000

        _, norm2 = PermitNormalizer.normalize_feature(feature_updated)
        new_cnt, upd_cnt, dup_cnt = permit_store.upsert_batch([norm2])

        assert new_cnt == 0
        assert upd_cnt == 1
        assert dup_cnt == 0
        assert permit_store.count() == 1  # Total row count remains 1

        # Verify updated values in database
        saved = permit_store.get_by_object_id("miami_dade_arcgis", 1001)
        assert saved["status"] == "F"
        assert saved["contractor_name"] == "SUPERIOR ROOFING LLC"
        assert saved["completion_at"] is not None

    def test_get_latest_issued_date(self, permit_store, sample_arcgis_feature):
        _, norm = PermitNormalizer.normalize_feature(sample_arcgis_feature)
        permit_store.upsert_batch([norm])

        latest = permit_store.get_latest_issued_date("miami_dade_arcgis")
        assert latest == "2024-01-01T00:00:00Z"

    def test_export_to_json(self, permit_store, sample_arcgis_feature, tmp_path):
        _, norm = PermitNormalizer.normalize_feature(sample_arcgis_feature)
        permit_store.upsert_batch([norm])

        json_out = tmp_path / "permits_test.json"
        exported_count = permit_store.export_to_json(json_out)

        assert exported_count == 1
        assert json_out.exists()
        assert "ROOFING - TILE" in json_out.read_text(encoding="utf-8")


class TestStateStore:
    def test_checkpoint_lifecycle(self, state_store):
        assert state_store.get_state("miami_dade_arcgis") is None

        state_store.update_checkpoint(
            job_id="miami_dade_arcgis",
            last_processed_date="2024-01-01T00:00:00Z",
            total_ingested=100,
            status="completed",
            metadata={"source": "test"},
        )

        state = state_store.get_state("miami_dade_arcgis")
        assert state is not None
        assert state["last_processed_date"] == "2024-01-01T00:00:00Z"
        assert state["total_ingested"] == 100
        assert state["status"] == "completed"
        assert state["metadata"]["source"] == "test"
