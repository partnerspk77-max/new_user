"""
Outcome Tracking & Feedback Loop Engine.
Captures contractor interactions, utility markings, appointments, and sales conversions
to build a proprietary, conversion-weighted ranking dataset.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.logger import logger
from src.storage.database import Database


@dataclass
class SignalOutcome:
    """Represents customer/contractor interaction outcome on a commercial signal."""

    signal_id: str
    folio: str
    reviewer_or_contractor: str  # Contractor name or sales rep ID
    outcome_id: Optional[str] = None
    shown_to_customer_at: Optional[str] = None
    customer_viewed: bool = False
    customer_exported: bool = False
    customer_contacted: bool = False
    customer_marked_useful: bool = False
    customer_marked_bad: bool = False
    appointment: bool = False
    won: bool = False
    lost: bool = False
    disposition: str = "PENDING"  # 'PENDING', 'INTERESTED', 'REJECTED', 'CONVERTED', 'UNRESPONSIVE'
    feedback_notes: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self):
        if not self.outcome_id:
            key = f"{self.signal_id}:{self.reviewer_or_contractor}"
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()
            self.outcome_id = f"OUT-{digest}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OutcomeStorage:
    """Repository managing contractor behavioral telemetry and lead conversion outcomes."""

    def __init__(self, db: Database):
        self.db = db
        self.init_schema()

    def init_schema(self) -> None:
        """Initializes signal_outcomes table and indexes."""
        from src.opportunity.storage import OpportunityStorage
        OpportunityStorage(self.db)

        conn = self.db.get_connection()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    folio TEXT NOT NULL,
                    reviewer_or_contractor TEXT NOT NULL,
                    shown_to_customer_at TEXT,
                    customer_viewed INTEGER DEFAULT 0,
                    customer_exported INTEGER DEFAULT 0,
                    customer_contacted INTEGER DEFAULT 0,
                    customer_marked_useful INTEGER DEFAULT 0,
                    customer_marked_bad INTEGER DEFAULT 0,
                    appointment INTEGER DEFAULT 0,
                    won INTEGER DEFAULT 0,
                    lost INTEGER DEFAULT 0,
                    disposition TEXT NOT NULL DEFAULT 'PENDING',
                    feedback_notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_out_signal ON signal_outcomes (signal_id);
                CREATE INDEX IF NOT EXISTS idx_out_contractor ON signal_outcomes (reviewer_or_contractor);
                CREATE INDEX IF NOT EXISTS idx_out_disp ON signal_outcomes (disposition);
                CREATE INDEX IF NOT EXISTS idx_out_won ON signal_outcomes (won);
                """
            )

    def record_outcome(self, outcome: SignalOutcome) -> str:
        """Persists or statefully updates contractor outcome telemetry."""
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self.db.get_connection()

        with conn:
            conn.execute(
                """
                INSERT INTO signal_outcomes (
                    outcome_id, signal_id, folio, reviewer_or_contractor,
                    shown_to_customer_at, customer_viewed, customer_exported,
                    customer_contacted, customer_marked_useful, customer_marked_bad,
                    appointment, won, lost, disposition, feedback_notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(outcome_id) DO UPDATE SET
                    shown_to_customer_at = COALESCE(excluded.shown_to_customer_at, signal_outcomes.shown_to_customer_at),
                    customer_viewed = MAX(signal_outcomes.customer_viewed, excluded.customer_viewed),
                    customer_exported = MAX(signal_outcomes.customer_exported, excluded.customer_exported),
                    customer_contacted = MAX(signal_outcomes.customer_contacted, excluded.customer_contacted),
                    customer_marked_useful = MAX(signal_outcomes.customer_marked_useful, excluded.customer_marked_useful),
                    customer_marked_bad = MAX(signal_outcomes.customer_marked_bad, excluded.customer_marked_bad),
                    appointment = MAX(signal_outcomes.appointment, excluded.appointment),
                    won = MAX(signal_outcomes.won, excluded.won),
                    lost = MAX(signal_outcomes.lost, excluded.lost),
                    disposition = excluded.disposition,
                    feedback_notes = COALESCE(excluded.feedback_notes, signal_outcomes.feedback_notes),
                    updated_at = excluded.updated_at
                """,
                (
                    outcome.outcome_id,
                    outcome.signal_id,
                    outcome.folio,
                    outcome.reviewer_or_contractor,
                    outcome.shown_to_customer_at,
                    1 if outcome.customer_viewed else 0,
                    1 if outcome.customer_exported else 0,
                    1 if outcome.customer_contacted else 0,
                    1 if outcome.customer_marked_useful else 0,
                    1 if outcome.customer_marked_bad else 0,
                    1 if outcome.appointment else 0,
                    1 if outcome.won else 0,
                    1 if outcome.lost else 0,
                    outcome.disposition,
                    outcome.feedback_notes,
                    outcome.created_at or now_iso,
                    now_iso,
                ),
            )

        logger.info(f"Recorded outcome {outcome.outcome_id} for signal {outcome.signal_id} ({outcome.disposition}).")
        return outcome.outcome_id

    def get_outcomes_for_signal(self, signal_id: str) -> List[SignalOutcome]:
        """Retrieves all feedback records for a specific signal."""
        conn = self.db.get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT outcome_id, signal_id, folio, reviewer_or_contractor,
                   shown_to_customer_at, customer_viewed, customer_exported,
                   customer_contacted, customer_marked_useful, customer_marked_bad,
                   appointment, won, lost, disposition, feedback_notes,
                   created_at, updated_at
            FROM signal_outcomes
            WHERE signal_id = ?
            """,
            (signal_id,),
        )
        return [self._row_to_outcome(r) for r in cur.fetchall()]

    def get_conversion_metrics(self) -> Dict[str, Any]:
        """Calculates global conversion rates and contractor validation stats."""
        conn = self.db.get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM signal_outcomes")
        total_tracked = cur.fetchone()[0]

        cur.execute("SELECT SUM(customer_viewed), SUM(customer_exported), SUM(customer_contacted), SUM(customer_marked_useful), SUM(customer_marked_bad), SUM(appointment), SUM(won), SUM(lost) FROM signal_outcomes")
        row = cur.fetchone()

        viewed = row[0] or 0
        exported = row[1] or 0
        contacted = row[2] or 0
        useful = row[3] or 0
        bad = row[4] or 0
        appointments = row[5] or 0
        won = row[6] or 0
        lost = row[7] or 0

        win_rate = round((won / (won + lost) * 100), 1) if (won + lost) > 0 else 0.0
        contact_rate = round((contacted / total_tracked * 100), 1) if total_tracked > 0 else 0.0

        return {
            "total_tracked": total_tracked,
            "viewed": viewed,
            "exported": exported,
            "contacted": contacted,
            "contact_rate_pct": contact_rate,
            "marked_useful": useful,
            "marked_bad": bad,
            "appointments": appointments,
            "deals_won": won,
            "deals_lost": lost,
            "win_rate_pct": win_rate,
        }

    @staticmethod
    def _row_to_outcome(row: Any) -> SignalOutcome:
        return SignalOutcome(
            outcome_id=row[0],
            signal_id=row[1],
            folio=row[2],
            reviewer_or_contractor=row[3],
            shown_to_customer_at=row[4],
            customer_viewed=bool(row[5]),
            customer_exported=bool(row[6]),
            customer_contacted=bool(row[7]),
            customer_marked_useful=bool(row[8]),
            customer_marked_bad=bool(row[9]),
            appointment=bool(row[10]),
            won=bool(row[11]),
            lost=bool(row[12]),
            disposition=row[13],
            feedback_notes=row[14],
            created_at=row[15],
            updated_at=row[16],
        )
