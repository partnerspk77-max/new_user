"""
Unit and integration tests for Roofing Intelligence & Classification.
"""

from unittest.mock import MagicMock, patch
import pytest

from src.roofing.classifier import RoofingClassifier
from src.roofing.detector import RoofingCandidateDetector
from src.roofing.models import PermitTextNormalizer
from src.roofing.profiler import RoofingProfiler
from src.roofing.storage import RoofingStorage
from src.storage.database import Database
from src.storage.permit_store import PermitStore
from src.pipeline.normalizer import PermitNormalizer


@pytest.fixture
def roofing_db():
    db = Database("sqlite://:memory:")
    db.initialize_schema()
    yield db
    db.close()


@pytest.fixture
def roofing_storage(roofing_db):
    return RoofingStorage(roofing_db)


@pytest.fixture
def sample_shingle_permit():
    return {
        "id": "miami_dade_arcgis:101",
        "source": "miami_dade_arcgis",
        "source_object_id": 101,
        "global_id": "{GUID-101}",
        "folio": "30-4015-001-0010",
        "permit_number": "20240101",
        "process_number": "M202401",
        "address": "450 CORAL WAY",
        "permit_type": "BLDG",
        "category_1": "0095",
        "description_1": "ASPHALT (FIBERGLASS) SHINGLE ROOFS",
        "comment": "COMPLETE TEAR OFF AND REROOF",
        "residential_commercial": "RESIDENTIAL",
        "proposed_use": "SINGLE FAMILY",
        "contractor_name": "SUNSHINE ROOFING SERVICES INC",
        "status": "A",
        "issued_at": "2024-01-15T00:00:00Z",
    }


@pytest.fixture
def sample_commercial_permit():
    return {
        "id": "miami_dade_arcgis:102",
        "source": "miami_dade_arcgis",
        "source_object_id": 102,
        "global_id": "{GUID-102}",
        "folio": "30-4015-001-0020",
        "permit_number": "20240102",
        "process_number": "M202402",
        "address": "1200 BRICKELL AVE",
        "permit_type": "BLDG",
        "category_1": "0092",
        "description_1": "GRAVEL, SBS, SINGLE PLY, ETC.",
        "comment": "COMMERCIAL TPO REROOF",
        "residential_commercial": "COMMERCIAL",
        "proposed_use": "OFFICE BUILDING",
        "contractor_name": "SOUTH FLORIDA ROOFING SYSTEMS",
        "status": "A",
        "issued_at": "2024-01-16T00:00:00Z",
    }


@pytest.fixture
def sample_solar_permit():
    return {
        "id": "miami_dade_arcgis:103",
        "source": "miami_dade_arcgis",
        "source_object_id": 103,
        "global_id": "{GUID-103}",
        "folio": "30-4015-001-0030",
        "permit_number": "20240103",
        "process_number": "M202403",
        "address": "780 NW 42ND AVE",
        "permit_type": "ELEC",
        "category_1": "0034",
        "description_1": "SOLAR PHOTOVOLTAIC",
        "comment": "ROOF MOUNTED SOLAR PV PANELS",
        "residential_commercial": "RESIDENTIAL",
        "contractor_name": "TESLA ENERGY",
        "status": "A",
        "issued_at": "2024-01-17T00:00:00Z",
    }


@pytest.fixture
def sample_non_roofing_permit():
    return {
        "id": "miami_dade_arcgis:104",
        "source": "miami_dade_arcgis",
        "source_object_id": 104,
        "global_id": "{GUID-104}",
        "folio": "30-4015-001-0040",
        "permit_number": "20240104",
        "process_number": "M202404",
        "address": "900 FLAGLER ST",
        "permit_type": "PLUM",
        "category_1": "0001",
        "description_1": "PLUMBING",
        "comment": "SEWER LINE REPAIR",
        "contractor_name": "ABC PLUMBING",
        "status": "A",
        "issued_at": "2024-01-18T00:00:00Z",
    }


class TestTextNormalizer:
    def test_clean_text(self):
        assert PermitTextNormalizer.clean_text("  REROOF/SHINGLE - TILE  ") == "reroof shingle tile"
        assert PermitTextNormalizer.clean_text(None) == ""

    def test_create_normalized_document(self, sample_shingle_permit):
        doc = PermitTextNormalizer.create_normalized_document(sample_shingle_permit)
        assert "permit_type: bldg" in doc
        assert "category_1: 0095 - asphalt fiberglass shingle roofs" in doc
        assert "comment: complete tear off and reroof" in doc


class TestRoofingCandidateDetector:
    def test_detects_primary_shingle_category(self, sample_shingle_permit):
        detector = RoofingCandidateDetector()
        res = detector.evaluate(sample_shingle_permit)
        assert res.is_candidate is True
        assert any("0095" in r for r in res.candidate_reasons)
        assert res.candidate_score >= 0.6

    def test_detects_solar_rooftop_permit(self, sample_solar_permit):
        detector = RoofingCandidateDetector()
        res = detector.evaluate(sample_solar_permit)
        assert res.is_candidate is True
        assert any("roof" in r.lower() for r in res.candidate_reasons)

    def test_rejects_plumbing_permit(self, sample_non_roofing_permit):
        detector = RoofingCandidateDetector()
        res = detector.evaluate(sample_non_roofing_permit)
        assert res.is_candidate is False
        assert len(res.candidate_reasons) == 0


class TestRoofingClassifier:
    def test_classifies_reroof_rule(self, sample_shingle_permit):
        detector = RoofingCandidateDetector()
        classifier = RoofingClassifier()
        cand = detector.evaluate(sample_shingle_permit)

        result = classifier.classify(sample_shingle_permit, cand)
        assert result.is_roofing is True
        assert result.job_type == "REROOF"
        assert result.classification_source == "rule"
        assert result.confidence >= 0.95

    def test_classifies_commercial_roof(self, sample_commercial_permit):
        detector = RoofingCandidateDetector()
        classifier = RoofingClassifier()
        cand = detector.evaluate(sample_commercial_permit)

        result = classifier.classify(sample_commercial_permit, cand)
        assert result.is_roofing is True
        assert result.job_type == "COMMERCIAL_ROOF"
        assert result.classification_source == "rule"

    def test_classifies_solar_roof_related(self, sample_solar_permit):
        detector = RoofingCandidateDetector()
        classifier = RoofingClassifier()
        cand = detector.evaluate(sample_solar_permit)

        result = classifier.classify(sample_solar_permit, cand)
        assert result.is_roofing is True
        assert result.job_type == "SOLAR_ROOF_RELATED"

    def test_rejects_non_candidate(self, sample_non_roofing_permit):
        detector = RoofingCandidateDetector()
        classifier = RoofingClassifier()
        cand = detector.evaluate(sample_non_roofing_permit)

        result = classifier.classify(sample_non_roofing_permit, cand)
        assert result.is_roofing is False
        assert result.job_type == "NOT_ROOFING"

    def test_caching_behavior(self, roofing_storage, sample_shingle_permit):
        detector = RoofingCandidateDetector()
        classifier = RoofingClassifier(cache_store=roofing_storage)
        cand = detector.evaluate(sample_shingle_permit)

        res1 = classifier.classify(sample_shingle_permit, cand)
        # Second call should fetch directly from cache
        res2 = classifier.classify(sample_shingle_permit, cand)
        assert res1.job_type == res2.job_type
        assert res1.confidence == res2.confidence


class TestRoofingStorageAndProfiler:
    def test_upsert_and_quality_analysis(self, roofing_db, roofing_storage, sample_shingle_permit):
        detector = RoofingCandidateDetector()
        classifier = RoofingClassifier(cache_store=roofing_storage)
        cand = detector.evaluate(sample_shingle_permit)
        decision = classifier.classify(sample_shingle_permit, cand)

        roofing_storage.upsert_roofing_permit(sample_shingle_permit, decision)
        assert roofing_storage.count() == 1

        profiler = RoofingProfiler(roofing_db)
        quality = profiler.analyze_quality()

        assert quality["total_roofing_permits"] == 1
        assert quality["folio_fill_rate"] == 100.0
        assert quality["contractor_fill_rate"] == 100.0

    def test_export_csv_and_json(self, roofing_storage, sample_shingle_permit, tmp_path):
        detector = RoofingCandidateDetector()
        classifier = RoofingClassifier()
        cand = detector.evaluate(sample_shingle_permit)
        decision = classifier.classify(sample_shingle_permit, cand)
        roofing_storage.upsert_roofing_permit(sample_shingle_permit, decision)

        csv_p = tmp_path / "roofing.csv"
        json_p = tmp_path / "roofing.json"
        exported = roofing_storage.export_csv_and_json(csv_p, json_p)

        assert exported == 1
        assert csv_p.exists()
        assert json_p.exists()


class TestRoofingCLI:
    def test_cli_inspect_roofing(self):
        from src.roofing.cli import main
        with patch("sys.argv", ["cli.py", "inspect-roofing"]):
            assert main() == 0

    def test_cli_classify_roofing(self):
        from src.roofing.cli import main
        with patch("sys.argv", ["cli.py", "classify-roofing"]):
            assert main() == 0

    def test_cli_validate_roofing(self):
        from src.roofing.cli import main
        with patch("sys.argv", ["cli.py", "validate-roofing"]):
            assert main() == 0

    def test_cli_roofing_stats(self):
        from src.roofing.cli import main
        with patch("sys.argv", ["cli.py", "roofing-stats"]):
            assert main() == 0
