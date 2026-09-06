"""
Unit and integration tests for Contractor Outcome Tracking and Feedback Telemetry.
"""

import pytest

from src.opportunity.outcomes import OutcomeStorage, SignalOutcome
from src.storage.database import Database


class TestOutcomeStorage:
    @pytest.fixture
    def test_db(self):
        db = Database("sqlite:///:memory:")
        db.initialize_schema()
        return db

    def test_record_and_retrieve_outcome(self, test_db):
        storage = OutcomeStorage(test_db)

        outcome = SignalOutcome(
            signal_id="SIG-TEST123456",
            folio="3010010000010",
            reviewer_or_contractor="Apex Roofing LLC",
            customer_viewed=True,
            customer_marked_useful=True,
            customer_contacted=True,
            appointment=True,
            won=True,
            disposition="CONVERTED",
            feedback_notes="Homeowner ready for tile reroof estimate",
        )

        out_id = storage.record_outcome(outcome)
        assert out_id.startswith("OUT-")

        records = storage.get_outcomes_for_signal("SIG-TEST123456")
        assert len(records) == 1
        rec = records[0]
        assert rec.reviewer_or_contractor == "Apex Roofing LLC"
        assert rec.customer_viewed is True
        assert rec.customer_marked_useful is True
        assert rec.appointment is True
        assert rec.won is True
        assert rec.disposition == "CONVERTED"

    def test_stateful_telemetry_updates(self, test_db):
        storage = OutcomeStorage(test_db)

        # Step 1: Initial view and export
        o1 = SignalOutcome(
            signal_id="SIG-ABC",
            folio="F1",
            reviewer_or_contractor="Best Roofs Inc",
            customer_viewed=True,
            customer_exported=True,
        )
        storage.record_outcome(o1)

        # Step 2: Contractor follows up later and schedules appointment
        o2 = SignalOutcome(
            signal_id="SIG-ABC",
            folio="F1",
            reviewer_or_contractor="Best Roofs Inc",
            customer_contacted=True,
            appointment=True,
            disposition="INTERESTED",
        )
        storage.record_outcome(o2)

        records = storage.get_outcomes_for_signal("SIG-ABC")
        assert len(records) == 1
        r = records[0]
        # Previous customer_viewed and customer_exported must NOT be wiped!
        assert r.customer_viewed is True
        assert r.customer_exported is True
        assert r.customer_contacted is True
        assert r.appointment is True
        assert r.disposition == "INTERESTED"

    def test_conversion_metrics_calculation(self, test_db):
        storage = OutcomeStorage(test_db)

        outcomes = [
            SignalOutcome(signal_id="S1", folio="F1", reviewer_or_contractor="R1", customer_contacted=True, won=True),
            SignalOutcome(signal_id="S2", folio="F2", reviewer_or_contractor="R1", customer_contacted=True, lost=True),
            SignalOutcome(signal_id="S3", folio="F3", reviewer_or_contractor="R2", customer_marked_bad=True),
        ]
        for o in outcomes:
            storage.record_outcome(o)

        metrics = storage.get_conversion_metrics()
        assert metrics["total_tracked"] == 3
        assert metrics["contacted"] == 2
        assert metrics["deals_won"] == 1
        assert metrics["deals_lost"] == 1
        assert metrics["win_rate_pct"] == 50.0
