"""
Unit and integration tests for Miami-Dade Property Opportunity Engine.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import pytest

from src.opportunity.engine import PropertyOpportunityEngine
from src.opportunity.models import CommercialOpportunity, PropertyTimeline
from src.opportunity.storage import OpportunityStorage
from src.roofing.models import RoofingClassification
from src.roofing.storage import RoofingStorage
from src.storage.database import Database
from src.storage.permit_store import PermitStore


@pytest.fixture
def opp_db():
    db = Database("sqlite://:memory:")
    db.initialize_schema()
    yield db
    db.close()


@pytest.fixture
def sample_permits_dataset():
    now_iso = datetime.now(timezone.utc).isoformat()
    return [
        # Property 1: Folio 01 - Active roof permit with contractor
        {
            "id": "md:1",
            "source": "miami_dade_arcgis",
            "source_object_id": 1,
            "global_id": "{GUID-1}",
            "folio": "30-1001-000-0010",
            "permit_number": "20240001",
            "process_number": "M202401",
            "address": "100 OCEAN DR",
            "permit_type": "BLDG",
            "category_1": "0095",
            "description_1": "ASPHALT SHINGLE ROOFS",
            "comment": "COMPLETE REROOF",
            "contractor_name": "TITAN ROOFING LLC",
            "residential_commercial": "RESIDENTIAL",
            "status": "A",
            "issued_at": now_iso,
        },
        # Property 2: Folio 02 - Active owner-builder roof permit (NO CONTRACTOR)
        {
            "id": "md:2",
            "source": "miami_dade_arcgis",
            "source_object_id": 2,
            "global_id": "{GUID-2}",
            "folio": "30-1001-000-0020",
            "permit_number": "20240002",
            "process_number": "M202402",
            "address": "200 COLLINS AVE",
            "permit_type": "BLDG",
            "category_1": "0095",
            "description_1": "ASPHALT SHINGLE ROOFS",
            "comment": "OWNER BUILDER REROOF",
            "contractor_name": "OWNER-BUILDER",
            "residential_commercial": "RESIDENTIAL",
            "status": "A",
            "issued_at": now_iso,
        },
        # Property 3: Folio 03 - Multi-trade active renovation with NO roof permit
        {
            "id": "md:3",
            "source": "miami_dade_arcgis",
            "source_object_id": 3,
            "global_id": "{GUID-3}",
            "folio": "30-1001-000-0030",
            "permit_number": "20240003",
            "process_number": "M202403",
            "address": "300 BISCAYNE BLVD",
            "permit_type": "MECH",
            "category_1": "0003",
            "description_1": "AIR CONDITIONING",
            "comment": "REPLACE 5 TON A/C SYSTEM",
            "contractor_name": "COOL AIR INC",
            "residential_commercial": "RESIDENTIAL",
            "status": "A",
            "issued_at": now_iso,
        },
        {
            "id": "md:4",
            "source": "miami_dade_arcgis",
            "source_object_id": 4,
            "global_id": "{GUID-4}",
            "folio": "30-1001-000-0030",
            "permit_number": "20240004",
            "process_number": "M202404",
            "address": "300 BISCAYNE BLVD",
            "permit_type": "BLDG",
            "category_1": "0082",
            "description_1": "WINDOWS AND DOORS",
            "comment": "IMPACT WINDOW RETROFIT",
            "contractor_name": "IMPACT PROS LLC",
            "residential_commercial": "RESIDENTIAL",
            "status": "A",
            "issued_at": now_iso,
        },
        # Property 4: Folio 04 - Solar PV installation with NO roof permit
        {
            "id": "md:5",
            "source": "miami_dade_arcgis",
            "source_object_id": 5,
            "global_id": "{GUID-5}",
            "folio": "30-1001-000-0040",
            "permit_number": "20240005",
            "process_number": "M202405",
            "address": "400 SUNSET DR",
            "permit_type": "ELEC",
            "category_1": "0034",
            "description_1": "SOLAR PHOTOVOLTAIC",
            "comment": "INSTALL 10KW ROOFTOP SOLAR PV SYSTEM",
            "contractor_name": "SOLAR SUN INC",
            "residential_commercial": "RESIDENTIAL",
            "status": "A",
            "issued_at": now_iso,
        },
    ]


def populate_test_db(db: Database, permits: list):
    conn = db.get_connection()
    r_store = RoofingStorage(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        for p in permits:
            conn.execute(
                """
                INSERT INTO permits (
                    id, source, source_object_id, global_id, folio,
                    permit_number, process_number, address, permit_type,
                    category_1, description_1, comment, contractor_name,
                    residential_commercial, status, issued_at,
                    source_fetched_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["id"],
                    p["source"],
                    p["source_object_id"],
                    p["global_id"],
                    p["folio"],
                    p["permit_number"],
                    p["process_number"],
                    p["address"],
                    p["permit_type"],
                    p.get("category_1"),
                    p.get("description_1"),
                    p.get("comment"),
                    p.get("contractor_name"),
                    p.get("residential_commercial"),
                    p.get("status"),
                    p.get("issued_at"),
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )
            if "REROOF" in p.get("comment", ""):
                r_store.upsert_roofing_permit(
                    p,
                    RoofingClassification(
                        is_roofing=True,
                        job_type="REROOF",
                        confidence=0.98,
                        reason="Confirmed reroof permit",
                        classification_source="rule",
                    ),
                )


class TestPropertyOpportunityEngine:
    def test_build_property_timelines(self, opp_db, sample_permits_dataset):
        populate_test_db(opp_db, sample_permits_dataset)
        engine = PropertyOpportunityEngine(opp_db)
        timelines = engine.build_property_timelines()

        assert len(timelines) == 4
        # Folio 03 should have 2 permits and 2 trades (BLDG, MECH)
        t3 = timelines["30-1001-000-0030"]
        assert t3.total_permits == 2
        assert set(t3.trades) == {"BLDG", "MECH"}
        assert t3.roof_permits_count == 0
        assert t3.has_active_non_roof_permit is True
        assert "HVAC_AC_REPLACEMENT" in t3.non_roof_renovation_types
        assert "WINDOW_DOOR_RETROFIT" in t3.non_roof_renovation_types

    def test_generate_opportunities_dual_tracks(self, opp_db, sample_permits_dataset):
        populate_test_db(opp_db, sample_permits_dataset)
        engine = PropertyOpportunityEngine(opp_db)
        timelines = engine.build_property_timelines()
        opportunities = engine.generate_opportunities(timelines)

        opp_types = [o.opportunity_type for o in opportunities]
        audiences = [o.target_audience for o in opportunities]

        # Track A: Supplier opportunity for Folio 01
        assert "NEW_ROOF_PERMIT" in opp_types
        assert "SUPPLIER_DISTRIBUTOR" in audiences

        # Track B: Unassigned Owner-builder roof permit for Folio 02
        assert "UNASSIGNED_ROOF_PERMIT" in opp_types
        owner_opp = next(o for o in opportunities if o.opportunity_type == "UNASSIGNED_ROOF_PERMIT")
        assert owner_opp.target_audience == "ROOFING_CONTRACTOR"
        assert owner_opp.contractor_present is False
        assert owner_opp.priority_score >= 90.0

        # Track B: Multi-trade renovation without roof for Folio 03
        assert "PROPERTY_RENOVATION_OPPORTUNITY" in opp_types
        reno_opp = next(o for o in opportunities if o.opportunity_type == "PROPERTY_RENOVATION_OPPORTUNITY")
        assert reno_opp.target_audience == "ROOFING_CONTRACTOR"
        assert "Active modernization in progress" in reno_opp.trigger_event

        # Track B: Solar reroof opportunity for Folio 04
        assert "SOLAR_REROOF_OPPORTUNITY" in opp_types
        solar_opp = next(o for o in opportunities if o.opportunity_type == "SOLAR_REROOF_OPPORTUNITY")
        assert solar_opp.target_audience == "ROOFING_CONTRACTOR"
        assert solar_opp.priority_score >= 90.0

    def test_freshness_hour_precision(self, opp_db):
        engine = PropertyOpportunityEngine(opp_db)
        now_dt = datetime.now(timezone.utc)
        engine.ref_dt = now_dt

        # Exactly 5 hours ago
        from datetime import timedelta
        five_hours_ago = (now_dt - timedelta(hours=5)).isoformat()
        h, d, tier = engine._calculate_freshness(five_hours_ago)
        assert 4.9 <= h <= 5.1
        assert tier == "NEW (0-24h)"

        # 48 hours ago
        two_days_ago = (now_dt - timedelta(days=2)).isoformat()
        h2, d2, tier2 = engine._calculate_freshness(two_days_ago)
        assert 47.9 <= h2 <= 48.1
        assert tier2 == "FRESH (1-3d)"


class TestOpportunityStorageAndCLI:
    def test_storage_lifecycle_and_export(self, opp_db, sample_permits_dataset, tmp_path):
        populate_test_db(opp_db, sample_permits_dataset)
        storage = OpportunityStorage(opp_db)
        engine = PropertyOpportunityEngine(opp_db)

        timelines = engine.build_property_timelines()
        assert storage.save_timelines(timelines) == 4

        opps = engine.generate_opportunities(timelines)
        assert storage.save_opportunities(opps) == len(opps)

        # Query by audience
        roofers = storage.get_opportunities(audience="roofer")
        assert len(roofers) > 0
        assert all(r["target_audience"] == "ROOFING_CONTRACTOR" for r in roofers)

        suppliers = storage.get_opportunities(audience="supplier")
        assert len(suppliers) > 0
        assert all(r["target_audience"] == "SUPPLIER_DISTRIBUTOR" for r in suppliers)

        # Export test
        csv_r = tmp_path / "roofers.csv"
        csv_s = tmp_path / "suppliers.csv"
        json_a = tmp_path / "all.json"
        res = storage.export_opportunities(csv_r, csv_s, json_a)

        assert res["total"] == len(opps)
        assert csv_r.exists()
        assert csv_s.exists()
        assert json_a.exists()

    def test_cli_subcommands(self):
        from src.opportunity.cli import main
        with patch("sys.argv", ["cli.py", "build-graph"]):
            assert main() == 0

        with patch("sys.argv", ["cli.py", "list-opportunities", "--limit", "5"]):
            assert main() == 0

        with patch("sys.argv", ["cli.py", "opportunity-stats"]):
            assert main() == 0
