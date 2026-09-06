"""
Unit tests for PermitInspector discovery tooling.
"""

from src.discovery.inspector import PermitInspector
from src.pipeline.normalizer import PermitNormalizer


class TestPermitInspector:
    def test_inspect_empty_database(self, test_db):
        inspector = PermitInspector(test_db)
        stats = inspector.inspect_local()
        assert "error" in stats

    def test_inspect_populated_database(self, test_db, permit_store):
        # Feature 1: Complete with folio, contractor, geometry, status A
        feat1 = {
            "attributes": {
                "OBJECTID": 1,
                "ID": "P1",
                "PROCNUM": "PR1",
                "FOLIO": "30-1111",
                "TYPE": "BUILDING",
                "CAT1": "01",
                "DESC1": "ROOF REPAIR",
                "CAT2": "02",
                "DESC2": "PLUMBING",
                "ISSUDATE": 1704067200000,
                "CONTRNAME": "ROOFER 1",
                "BPSTATUS": "A",
            },
            "geometry": {"x": -80.19, "y": 25.76},
        }

        # Feature 2: Missing folio and geometry, status F
        feat2 = {
            "attributes": {
                "OBJECTID": 2,
                "ID": "P2",
                "PROCNUM": "PR2",
                "FOLIO": None,
                "TYPE": "ROOFING",
                "CAT1": "01",
                "DESC1": "ROOF REPAIR",
                "ISSUDATE": 1704153600000,
                "CONTRNAME": None,
                "BPSTATUS": "F",
            },
            "geometry": None,
        }

        _, norm1 = PermitNormalizer.normalize_feature(feat1)
        _, norm2 = PermitNormalizer.normalize_feature(feat2)
        permit_store.upsert_batch([norm1, norm2])

        inspector = PermitInspector(test_db)
        stats = inspector.inspect_local()

        assert stats["total_permits"] == 2
        # Completeness: 1/2 = 50%
        assert stats["G_folio_percentage"] == 50.0
        assert stats["H_contractor_percentage"] == 50.0
        assert stats["I_geometry_percentage"] == 50.0

        # Status distribution: 1 A, 1 F
        statuses = {s["status"]: s["count"] for s in stats["F_status_distribution"]}
        assert statuses.get("A") == 1
        assert statuses.get("F") == 1

        # Types
        types = {t["val"]: t["count"] for t in stats["A_permit_types"]}
        assert types.get("BUILDING") == 1
        assert types.get("ROOFING") == 1

        # Top descriptions: "ROOF REPAIR" appeared twice
        top_desc = stats["C_top_descriptions"][0]
        assert top_desc["description"] == "ROOF REPAIR"
        assert top_desc["count"] == 2

        # Verify print_report doesn't raise exception
        inspector.print_report(stats)
