"""
Unit and integration tests for Miami-Dade Property Signal Engine and Evidence Graph.
Tests stateful signal lifecycle, derived roof histories, and explainable scoring.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import pytest

from src.opportunity.engine import PropertyOpportunityEngine
from src.opportunity.models import PropertySignal, PropertyTimeline, SignalStatus, SignalType
from src.opportunity.storage import OpportunityStorage
from src.roofing.models import RoofingClassification
from src.roofing.storage import RoofingStorage
from src.storage.database import Database


@pytest.fixture
def opp_db():
    db = Database("sqlite://:memory:")
    db.initialize_schema()
    yield db
    db.close()


@pytest.fixture
def sample_permits_dataset():
    now_iso = datetime.now(timezone.utc).isoformat()
    past_iso = (datetime.now(timezone.utc) - timedelta(days=365 * 12)).isoformat()  # 12 years ago
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
        # Property 3: Folio 03 - Multi-trade active renovation + prior roof permit 12 years ago
        {
            "id": "md:3",
            "source": "miami_dade_arcgis",
            "source_object_id": 3,
            "global_id": "{GUID-3}",
            "folio": "30-1001-000-0030",
            "permit_number": "20120001",
            "process_number": "M201201",
            "address": "300 BISCAYNE BLVD",
            "permit_type": "BLDG",
            "category_1": "0107",
            "description_1": "CONCRETE TILE ROOF",
            "comment": "OLD TILE REROOF",
            "contractor_name": "HISTORIC ROOFING CO",
            "residential_commercial": "RESIDENTIAL",
            "status": "F",
            "issued_at": past_iso,
        },
        {
            "id": "md:4",
            "source": "miami_dade_arcgis",
            "source_object_id": 4,
            "global_id": "{GUID-4}",
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
            "id": "md:5",
            "source": "miami_dade_arcgis",
            "source_object_id": 5,
            "global_id": "{GUID-5}",
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
            "id": "md:6",
            "source": "miami_dade_arcgis",
            "source_object_id": 6,
            "global_id": "{GUID-6}",
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
    def test_build_property_timelines_and_roof_history(self, opp_db, sample_permits_dataset):
        populate_test_db(opp_db, sample_permits_dataset)
        engine = PropertyOpportunityEngine(opp_db)
        timelines = engine.build_property_timelines()

        assert len(timelines) == 4
        # Folio 03: 3 permits total, 1 historical roof permit (12 yrs ago), 2 active renovation permits
        t3 = timelines["30-1001-000-0030"]
        assert t3.total_permits == 3
        assert set(t3.trades) == {"BLDG", "MECH"}
        assert set(t3.active_trades) == {"BLDG", "MECH"}
        assert t3.roof_permits_count == 1
        assert t3.has_active_roof_permit is False  # Historical was finalized
        assert t3.has_active_non_roof_permit is True
        assert t3.years_since_last_roof_permit is not None
        assert 11.5 <= t3.years_since_last_roof_permit <= 12.5
        assert t3.last_roof_system == "CONCRETE_OR_CLAY_TILE"
        assert t3.last_roof_contractor == "HISTORIC ROOFING CO"

    def test_generate_signals_with_evidence_rubric(self, opp_db, sample_permits_dataset):
        populate_test_db(opp_db, sample_permits_dataset)
        engine = PropertyOpportunityEngine(opp_db)
        timelines = engine.build_property_timelines()
        signals = engine.generate_signals(timelines)

        sig_types = [s.signal_type for s in signals]
        audiences = [s.target_audience for s in signals]

        # Track A: Supplier Signal for Folio 01
        assert SignalType.NEW_ROOF_PERMIT in sig_types
        assert "SUPPLIER_DISTRIBUTOR" in audiences

        # Track B: Owner-Builder Roof Signal for Folio 02
        assert SignalType.OWNER_BUILDER_ROOF_SIGNAL in sig_types
        owner_sig = next(s for s in signals if s.signal_type == SignalType.OWNER_BUILDER_ROOF_SIGNAL)
        assert owner_sig.target_audience == "ROOFING_CONTRACTOR"
        assert owner_sig.contractor_present is False
        assert owner_sig.evidence_score >= 80.0
        assert len(owner_sig.unverified_assumptions) > 0

        # Track B: Multi-Trade Renovation Signal for Folio 03
        assert SignalType.PROPERTY_RENOVATION_SIGNAL in sig_types
        reno_sig = next(s for s in signals if s.signal_type == SignalType.PROPERTY_RENOVATION_SIGNAL)
        assert reno_sig.target_audience == "ROOFING_CONTRACTOR"
        assert "last recorded permit was" in " ".join(reno_sig.corroborating_signals)
        assert len(reno_sig.unverified_assumptions) >= 2
        assert reno_sig.evidence_breakdown["roof_history"] >= 20.0

        # Track B: Solar Roof Signal for Folio 04
        assert SignalType.SOLAR_ROOF_SIGNAL in sig_types
        solar_sig = next(s for s in signals if s.signal_type == SignalType.SOLAR_ROOF_SIGNAL)
        assert solar_sig.target_audience == "ROOFING_CONTRACTOR"
        assert solar_sig.evidence_score >= 80.0


class TestOpportunityStorageAndCLI:
    def test_stateful_signal_lifecycle(self, opp_db, sample_permits_dataset, tmp_path):
        populate_test_db(opp_db, sample_permits_dataset)
        storage = OpportunityStorage(opp_db)
        engine = PropertyOpportunityEngine(opp_db)

        timelines = engine.build_property_timelines()
        storage.save_timelines(timelines)

        signals_1 = engine.generate_signals(timelines)
        storage.save_signals(signals_1)

        # Verify initial insert
        active_signals = storage.get_signals(status=SignalStatus.ACTIVE)
        assert len(active_signals) == len(signals_1)
        first_detected_map = {s["signal_id"]: s["first_detected_at"] for s in active_signals}

        # Second rebuild: simulate slight time passage
        signals_2 = engine.generate_signals(timelines)
        storage.save_signals(signals_2)

        active_signals_2 = storage.get_signals(status=SignalStatus.ACTIVE)
        assert len(active_signals_2) == len(signals_2)
        for s in active_signals_2:
            # first_detected_at MUST be preserved!
            assert s["first_detected_at"] == first_detected_map[s["signal_id"]]

        # Test resolution: simulate removing one signal
        omitted_signals = signals_2[1:]  # Drop the first signal
        dropped_id = signals_2[0].signal_id
        storage.save_signals(omitted_signals)

        # Dropped signal should be RESOLVED, not deleted from the database!
        metrics = storage.get_metrics()
        assert metrics["active_signals"] == len(omitted_signals)
        assert metrics["resolved_signals"] == 1

        resolved = storage.get_signals(status=SignalStatus.RESOLVED)
        assert len(resolved) == 1
        assert resolved[0]["signal_id"] == dropped_id

    def test_cli_subcommands_and_exports(self, tmp_path):
        from src.opportunity.cli import main
        with patch("sys.argv", ["cli.py", "build-graph"]):
            assert main() == 0

        with patch("sys.argv", ["cli.py", "list-signals", "--limit", "5"]):
            assert main() == 0

        with patch("sys.argv", ["cli.py", "list-opportunities", "--limit", "5"]):
            assert main() == 0

        with patch("sys.argv", ["cli.py", "export-feeds"]):
            assert main() == 0

        with patch("sys.argv", ["cli.py", "export-validation-sample"]):
            assert main() == 0

        with patch("sys.argv", ["cli.py", "outcome-stats"]):
            assert main() == 0

    def test_commercial_feeds_and_market_intelligence(self, opp_db, sample_permits_dataset, tmp_path):
        populate_test_db(opp_db, sample_permits_dataset)
        storage = OpportunityStorage(opp_db)
        engine = PropertyOpportunityEngine(opp_db)

        timelines = engine.build_property_timelines()
        storage.save_timelines(timelines)
        signals = engine.generate_signals(timelines)
        storage.save_signals(signals)

        # Test market intelligence extraction
        intel = storage.get_contractor_market_intelligence(min_permits=1)
        assert len(intel) >= 1
        top_contractor = intel[0]
        assert "contractor_name" in top_contractor
        assert "recent_30d_permits" in top_contractor
        assert "velocity_growth_pct" in top_contractor
        assert "market_share_rank" in top_contractor

        # Test 3-feed export
        feed_res = storage.export_commercial_feeds(tmp_path)
        assert feed_res["feed_1_permitted_projects"] >= 1
        assert feed_res["feed_2_pre_permit_opportunities"] >= 1
        assert feed_res["feed_3_market_intelligence_contractors"] >= 1
        assert Path(feed_res["files"]["feed_1_csv"]).exists()
        assert Path(feed_res["files"]["feed_2_csv"]).exists()
        assert Path(feed_res["files"]["feed_3_csv"]).exists()


class TestParcelEnrichedOpportunityScoring:
    def test_building_age_and_scale_calibration(self, opp_db, sample_permits_dataset):
        populate_test_db(opp_db, sample_permits_dataset)
        engine = PropertyOpportunityEngine(opp_db)

        # Scenario 1: Older building (1955, age 71) with active AC permit and NO roof permit
        from src.enrichment.models import PropertyParcel
        older_parcel = PropertyParcel(
            folio="30-1001-000-0030",
            year_built=1955,
            building_actual_area=6200.0,
            owner_name="ESTATE HOLDINGS LLC",
            dor_desc="SINGLE FAMILY RESIDENTIAL",
            assessed_value=1500000.0,
        )

        timelines = engine.build_property_timelines(parcels_map={"3010010000030": older_parcel})
        t3 = timelines["30-1001-000-0030"]
        assert t3.year_built == 1955
        assert t3.building_age is not None and t3.building_age >= 70
        assert t3.estimated_roof_squares == 62.0
        assert t3.owner_name == "ESTATE HOLDINGS LLC"

        signals = engine.generate_signals(timelines)
        reno_sig = next(s for s in signals if s.folio == "30-1001-000-0030" and s.signal_type == SignalType.PROPERTY_RENOVATION_SIGNAL)

        # Folio 03 has a recorded roof permit from 12 years ago in sample data:
        # years_since_last_roof_permit = 12.0 -> 20.0 pts.
        # Plus scale points >= 5000 sq ft -> 10.0 pts.
        assert reno_sig.evidence_breakdown["property_scale"] == 10.0
        assert reno_sig.year_built == 1955
        assert reno_sig.owner_name == "ESTATE HOLDINGS LLC"

    def test_new_construction_vs_vintage_structure_scoring(self, opp_db):
        engine = PropertyOpportunityEngine(opp_db)
        from src.enrichment.models import PropertyParcel

        # Case A: Vintage home built 1960 (no roof permit on record)
        vintage_timeline = PropertyTimeline(
            folio="FOLIO-VINTAGE",
            address="100 OLD RD",
            total_permits=1,
            trades=["MECH"],
            active_trades=["MECH"],
            roof_permits_count=0,
            has_active_roof_permit=False,
            has_active_non_roof_permit=True,
            non_roof_renovation_types=["HVAC_AC_REPLACEMENT"],
            year_built=1960,
            building_actual_area=2400.0,
            permits=[{"permit_number": "M1", "permit_type": "MECH", "status": "A", "issued_at": "2026-09-01T00:00:00Z"}]
        )

        # Case B: Brand new construction built 2024 (no roof permit on record)
        new_timeline = PropertyTimeline(
            folio="FOLIO-NEW",
            address="200 NEW WAY",
            total_permits=1,
            trades=["MECH"],
            active_trades=["MECH"],
            roof_permits_count=0,
            has_active_roof_permit=False,
            has_active_non_roof_permit=True,
            non_roof_renovation_types=["HVAC_AC_REPLACEMENT"],
            year_built=2024,
            building_actual_area=2400.0,
            permits=[{"permit_number": "M2", "permit_type": "MECH", "status": "A", "issued_at": "2026-09-01T00:00:00Z"}]
        )

        timelines = {"FOLIO-VINTAGE": vintage_timeline, "FOLIO-NEW": new_timeline}
        signals = engine.generate_signals(timelines)

        vintage_sig = next(s for s in signals if s.folio == "FOLIO-VINTAGE")
        new_sig = next(s for s in signals if s.folio == "FOLIO-NEW")

        # Vintage structure built in 1960 (66 yrs old) must receive 30 points for aging structure
        assert vintage_sig.evidence_breakdown["roof_history"] == 30.0
        assert "Structure built in 1960" in " ".join(vintage_sig.corroborating_signals)

        # New construction built in 2024 (2 yrs old) must receive 0 points (disqualified)
        assert new_sig.evidence_breakdown["roof_history"] == 0.0
        assert "Recent construction" in " ".join(new_sig.corroborating_signals)

        # Vintage score must be significantly higher than new construction score
        assert vintage_sig.evidence_score > new_sig.evidence_score

    def test_storm_evidence_and_epistemic_status(self, opp_db):
        from src.weather.correlator import StormCorrelator
        from src.weather.models import StormEvent

        storm = StormEvent(
            event_id="STM-TEST01",
            event_type="HAIL",
            magnitude=1.75,
            unit="INCHES",
            event_time="2026-08-25T14:00:00Z",
            latitude=25.7600,
            longitude=-80.2000,
        )
        correlator = StormCorrelator([storm], max_distance_miles=10.0)
        engine = PropertyOpportunityEngine(opp_db, storm_correlator=correlator)

        # Property at (25.7620, -80.2050) ~ 0.3 miles from severe hail
        timeline = PropertyTimeline(
            folio="FOLIO-STORM-HAIL",
            address="500 SW 1 ST",
            total_permits=1,
            trades=["MECH"],
            active_trades=["MECH"],
            roof_permits_count=0,
            has_active_roof_permit=False,
            has_active_non_roof_permit=True,
            non_roof_renovation_types=["HVAC_AC_REPLACEMENT"],
            year_built=1978,
            building_actual_area=2200.0,
            permits=[{"permit_number": "M-STORM", "permit_type": "MECH", "status": "A", "issued_at": "2026-09-01T00:00:00Z", "latitude": 25.7620, "longitude": -80.2050}]
        )

        signals = engine.generate_signals({"FOLIO-STORM-HAIL": timeline})
        assert len(signals) == 1
        sig = signals[0]

        # Epistemic status checks
        assert sig.year_built_status == "VERIFIED"
        assert sig.roof_history_status == "NO_PERMIT_IN_DATASET_WINDOW"
        assert sig.year_built == 1978

        # Storm correlation checks
        assert sig.storm_event_type == "HAIL"
        assert sig.storm_distance_miles is not None and sig.storm_distance_miles <= 1.0
        assert sig.evidence_breakdown["storm_evidence"] >= 15.0
        assert "Recent severe HAIL" in " ".join(sig.corroborating_signals)
