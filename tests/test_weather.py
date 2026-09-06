"""
Unit and integration tests for NOAA / NWS severe weather and storm correlation.
"""

from unittest.mock import MagicMock
import pytest

from src.storage.database import Database
from src.weather.client import NOAAStormClient
from src.weather.correlator import StormCorrelator, haversine_distance_miles
from src.weather.models import StormEvent
from src.weather.storage import StormStorage


class TestStormModelAndDistance:
    def test_storm_event_model_and_severity(self):
        # Severe hail event
        hail = StormEvent(
            event_id="E1",
            event_type="HAIL",
            magnitude=1.5,
            unit="INCHES",
            latitude=25.76,
            longitude=-80.19,
        )
        assert hail.is_severe is True

        # Non-severe small pea hail
        small_hail = StormEvent(
            event_id="E2",
            event_type="HAIL",
            magnitude=0.25,
            unit="INCHES",
        )
        assert small_hail.is_severe is False

        # Severe wind damage
        wind = StormEvent(
            event_id="E3",
            event_type="TSTM WND DMG",
            magnitude=60.0,
            unit="MPH",
        )
        assert wind.is_severe is True

    def test_nws_lsr_feature_parsing(self):
        raw_feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-80.28, 25.92]},
            "properties": {
                "wfo": "MFL",
                "typetext": "HAIL",
                "magf": 1.25,
                "unit": "INCHES",
                "county": "Miami-Dade",
                "city": "Miami Lakes",
                "remark": "Quarter size hail damaged asphalt roof shingles",
                "valid": "2024-06-15T18:30:00Z",
            },
        }

        ev = StormEvent.from_nws_lsr_feature(raw_feature)
        assert ev is not None
        assert ev.event_type == "HAIL"
        assert ev.magnitude == 1.25
        assert ev.unit == "INCHES"
        assert ev.city == "Miami Lakes"
        assert ev.county == "Miami-Dade"
        assert ev.latitude == 25.92
        assert ev.longitude == -80.28
        assert ev.is_severe is True

    def test_haversine_distance_calculation(self):
        # Identical point = 0 miles
        assert haversine_distance_miles(25.7617, -80.1918, 25.7617, -80.1918) == 0.0

        # Miami Downtown (25.7617, -80.1918) to Miami Beach (25.7907, -80.1300) ~ 4.3 miles
        dist = haversine_distance_miles(25.7617, -80.1918, 25.7907, -80.1300)
        assert 3.5 <= dist <= 5.5


class TestStormCorrelatorAndStorage:
    @pytest.fixture
    def test_db(self):
        db = Database("sqlite:///:memory:")
        db.initialize_schema()
        return db

    def test_correlator_proximity_matching(self):
        events = [
            StormEvent(
                event_id="S1",
                event_type="HAIL",
                magnitude=1.75,
                latitude=25.7500,
                longitude=-80.2000,
                event_time="2026-08-01T15:00:00Z",
            ),
            StormEvent(
                event_id="S2",
                event_type="TSTM WND DMG",
                magnitude=65.0,
                latitude=26.1000,
                longitude=-80.1500,  # Fort Lauderdale (~24 miles away)
                event_time="2026-08-01T15:00:00Z",
            ),
        ]

        correlator = StormCorrelator(events, max_distance_miles=10.0)

        # Property close to S1 (less than 2 miles)
        match = correlator.find_nearest_storm(25.7600, -80.2100)
        assert match is not None
        ev, dist, age = match
        assert ev.event_id == "S1"
        assert dist < 2.0
        assert age >= 0.0

        # Property far away (Homestead / 20 miles away) -> returns None due to 10-mile threshold
        far_match = correlator.find_nearest_storm(25.4687, -80.4776)
        assert far_match is None

    def test_storage_crud_and_stats(self, test_db):
        storage = StormStorage(test_db)
        events = [
            StormEvent(event_id="E1", event_type="HAIL", magnitude=1.0, latitude=25.7, longitude=-80.2, event_time="2024-05-01T00:00Z"),
            StormEvent(event_id="E2", event_type="TORNADO", magnitude=0.0, latitude=25.8, longitude=-80.3, event_time="2024-06-01T00:00Z"),
        ]

        saved = storage.upsert_storm_events(events)
        assert saved == 2

        all_events = storage.get_all_storm_events()
        assert len(all_events) == 2

        stats = storage.get_stats()
        assert stats["total_events"] == 2
        assert stats["by_type"]["HAIL"] == 1
        assert stats["by_type"]["TORNADO"] == 1
