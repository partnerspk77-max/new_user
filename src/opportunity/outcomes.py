"""
Outcome Tracking & Feedback Loop Engine.
Captures contractor interactions, utility markings, appointments, and sales conversions
to build a proprietary, conversion-weighted ranking dataset with lead-to-job attribution.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.logger import logger
from src.storage.database import Database

POSITIVE_REASONS = {
    "confirmed_leak",
    "confirmed_old_roof",
    "homeowner_interested",
    "inspection_booked",
    "insurance_claim",
}

NEGATIVE_REASONS = {
    "roof_already_replaced",
    "wrong_property",
    "commercial_not_relevant",
    "duplicate",
    "too_old",
    "storm_signal_inaccurate",
    "no_owner_contact",
}

ALL_REASON_CODES = POSITIVE_REASONS | NEGATIVE_REASONS


@dataclass
class SignalOutcome:
    """Represents customer/contractor interaction outcome on a commercial signal."""

    signal_id: str
    folio: str
    reviewer_or_contractor: str  # Contractor name or sales rep ID
    outcome_id: Optional[str] = None
    shown_to_customer_at: Optional[str] = None
    customer_viewed: bool = False
    contractor_selected: bool = False  # Step 1: Roofer picked property to pursue
    customer_exported: bool = False
    customer_contacted: bool = False   # Step 2: Roofer contacted homeowner
    customer_marked_useful: bool = False
    customer_marked_bad: bool = False
    appointment: bool = False          # Step 3: Onsite inspection booked
    estimate_amount: Optional[float] = None  # Step 4: Dollar quote delivered
    won: bool = False                  # Step 5: Deal won / contract signed
    won_amount: Optional[float] = None       # Dollar revenue awarded
    lost: bool = False
    disposition: str = "PENDING"  # 'PENDING', 'INTERESTED', 'REJECTED', 'CONVERTED', 'UNRESPONSIVE'
    reason_code: Optional[str] = None  # Structured reason code (from POSITIVE_REASONS or NEGATIVE_REASONS)
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

        if self.reason_code:
            self.reason_code = self.reason_code.strip().lower()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OutcomeStorage:
    """Repository managing contractor behavioral telemetry and lead conversion outcomes."""

    def __init__(self, db: Database):
        self.db = db
        self.init_schema()

    def init_schema(self) -> None:
        """Initializes signal_outcomes table, indexes, and ensures schema migrations."""
        from src.opportunity.storage import OpportunityStorage
        OpportunityStorage(self.db)

        conn = self.db.get_connection()
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    folio TEXT NOT NULL,
                    reviewer_or_contractor TEXT NOT NULL,
                    shown_to_customer_at TEXT,
                    customer_viewed INTEGER DEFAULT 0,
                    contractor_selected INTEGER DEFAULT 0,
                    customer_exported INTEGER DEFAULT 0,
                    customer_contacted INTEGER DEFAULT 0,
                    customer_marked_useful INTEGER DEFAULT 0,
                    customer_marked_bad INTEGER DEFAULT 0,
                    appointment INTEGER DEFAULT 0,
                    estimate_amount REAL,
                    won INTEGER DEFAULT 0,
                    won_amount REAL,
                    lost INTEGER DEFAULT 0,
                    disposition TEXT NOT NULL DEFAULT 'PENDING',
                    reason_code TEXT,
                    feedback_notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # Check existing columns to apply migrations if table already existed
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(signal_outcomes)")
            existing_cols = {r[1] for r in cursor.fetchall()}
            migrations = [
                ("contractor_selected", "INTEGER DEFAULT 0"),
                ("estimate_amount", "REAL"),
                ("won_amount", "REAL"),
                ("reason_code", "TEXT"),
            ]
            for col_name, col_type in migrations:
                if col_name not in existing_cols:
                    cursor.execute(f"ALTER TABLE signal_outcomes ADD COLUMN {col_name} {col_type}")

            # Create indexes safely after columns exist
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_out_signal ON signal_outcomes (signal_id);
                CREATE INDEX IF NOT EXISTS idx_out_contractor ON signal_outcomes (reviewer_or_contractor);
                CREATE INDEX IF NOT EXISTS idx_out_disp ON signal_outcomes (disposition);
                CREATE INDEX IF NOT EXISTS idx_out_won ON signal_outcomes (won);
                CREATE INDEX IF NOT EXISTS idx_out_selected ON signal_outcomes (contractor_selected);
                CREATE INDEX IF NOT EXISTS idx_out_reason ON signal_outcomes (reason_code);
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
                    shown_to_customer_at, customer_viewed, contractor_selected,
                    customer_exported, customer_contacted, customer_marked_useful,
                    customer_marked_bad, appointment, estimate_amount, won,
                    won_amount, lost, disposition, reason_code, feedback_notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(outcome_id) DO UPDATE SET
                    shown_to_customer_at = COALESCE(excluded.shown_to_customer_at, signal_outcomes.shown_to_customer_at),
                    customer_viewed = MAX(signal_outcomes.customer_viewed, excluded.customer_viewed),
                    contractor_selected = MAX(signal_outcomes.contractor_selected, excluded.contractor_selected),
                    customer_exported = MAX(signal_outcomes.customer_exported, excluded.customer_exported),
                    customer_contacted = MAX(signal_outcomes.customer_contacted, excluded.customer_contacted),
                    customer_marked_useful = MAX(signal_outcomes.customer_marked_useful, excluded.customer_marked_useful),
                    customer_marked_bad = MAX(signal_outcomes.customer_marked_bad, excluded.customer_marked_bad),
                    appointment = MAX(signal_outcomes.appointment, excluded.appointment),
                    estimate_amount = COALESCE(excluded.estimate_amount, signal_outcomes.estimate_amount),
                    won = MAX(signal_outcomes.won, excluded.won),
                    won_amount = COALESCE(excluded.won_amount, signal_outcomes.won_amount),
                    lost = MAX(signal_outcomes.lost, excluded.lost),
                    disposition = excluded.disposition,
                    reason_code = COALESCE(excluded.reason_code, signal_outcomes.reason_code),
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
                    1 if outcome.contractor_selected else 0,
                    1 if outcome.customer_exported else 0,
                    1 if outcome.customer_contacted else 0,
                    1 if outcome.customer_marked_useful else 0,
                    1 if outcome.customer_marked_bad else 0,
                    1 if outcome.appointment else 0,
                    outcome.estimate_amount,
                    1 if outcome.won else 0,
                    outcome.won_amount,
                    1 if outcome.lost else 0,
                    outcome.disposition,
                    outcome.reason_code,
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
                   shown_to_customer_at, customer_viewed, contractor_selected,
                   customer_exported, customer_contacted, customer_marked_useful,
                   customer_marked_bad, appointment, estimate_amount, won,
                   won_amount, lost, disposition, reason_code, feedback_notes,
                   created_at, updated_at
            FROM signal_outcomes
            WHERE signal_id = ?
            """,
            (signal_id,),
        )
        return [self._row_to_outcome(r) for r in cur.fetchall()]

    def get_conversion_metrics(self) -> Dict[str, Any]:
        """Calculates conversion funnel, revenue attribution, reason breakdown, and score calibration."""
        conn = self.db.get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM signal_outcomes")
        total_tracked = cur.fetchone()[0]

        cur.execute(
            """
            SELECT
                SUM(customer_viewed),
                SUM(contractor_selected),
                SUM(customer_exported),
                SUM(customer_contacted),
                SUM(customer_marked_useful),
                SUM(customer_marked_bad),
                SUM(appointment),
                SUM(estimate_amount),
                SUM(won),
                SUM(won_amount),
                SUM(lost)
            FROM signal_outcomes
            """
        )
        row = cur.fetchone()

        viewed = row[0] or 0
        selected = row[1] or 0
        exported = row[2] or 0
        contacted = row[3] or 0
        useful = row[4] or 0
        bad = row[5] or 0
        appointments = row[6] or 0
        total_estimate_dollars = row[7] or 0.0
        won = row[8] or 0
        total_won_dollars = row[9] or 0.0
        lost = row[10] or 0

        win_rate = round((won / (won + lost) * 100), 1) if (won + lost) > 0 else 0.0
        selection_rate = round((selected / viewed * 100), 1) if viewed > 0 else 0.0
        contact_rate = round((contacted / selected * 100), 1) if selected > 0 else (
            round((contacted / total_tracked * 100), 1) if total_tracked > 0 else 0.0
        )
        appointment_rate = round((appointments / contacted * 100), 1) if contacted > 0 else 0.0

        # Breakdown by reason code
        cur.execute(
            """
            SELECT reason_code, COUNT(*) as cnt
            FROM signal_outcomes
            WHERE reason_code IS NOT NULL AND reason_code != ''
            GROUP BY reason_code
            ORDER BY cnt DESC
            """
        )
        by_reason = {r[0]: r[1] for r in cur.fetchall()}

        # Performance grouped by evidence score tier
        cur.execute(
            """
            SELECT
                CASE
                    WHEN s.evidence_score >= 90.0 THEN 'Tier 1 (Score 90+)'
                    WHEN s.evidence_score >= 70.0 THEN 'Tier 2 (Score 70-89)'
                    ELSE 'Tier 3 (Score < 70)'
                END as score_tier,
                COUNT(o.outcome_id) as total,
                SUM(o.contractor_selected) as selected,
                SUM(o.appointment) as appointments,
                SUM(o.won) as won,
                SUM(COALESCE(o.won_amount, 0.0)) as won_dollars
            FROM signal_outcomes o
            LEFT JOIN property_signals s ON o.signal_id = s.signal_id
            GROUP BY score_tier
            ORDER BY score_tier ASC
            """
        )
        by_score_tier = {}
        for r in cur.fetchall():
            tier_name = r[0]
            t_total = r[1]
            t_sel = r[2] or 0
            t_app = r[3] or 0
            t_won = r[4] or 0
            t_rev = r[5] or 0.0
            by_score_tier[tier_name] = {
                "total": t_total,
                "selected": t_sel,
                "appointments": t_app,
                "won": t_won,
                "won_revenue": t_rev,
                "appointment_rate_pct": round((t_app / t_sel * 100), 1) if t_sel > 0 else 0.0,
                "win_rate_pct": round((t_won / t_sel * 100), 1) if t_sel > 0 else 0.0,
            }

        return {
            "total_tracked": total_tracked,
            "viewed": viewed,
            "selected": selected,
            "selection_rate_pct": selection_rate,
            "exported": exported,
            "contacted": contacted,
            "contact_rate_pct": contact_rate,
            "marked_useful": useful,
            "marked_bad": bad,
            "appointments": appointments,
            "appointment_rate_pct": appointment_rate,
            "total_estimate_dollars": total_estimate_dollars,
            "deals_won": won,
            "deals_lost": lost,
            "win_rate_pct": win_rate,
            "total_won_dollars": total_won_dollars,
            "by_reason": by_reason,
            "by_score_tier": by_score_tier,
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
            contractor_selected=bool(row[6]),
            customer_exported=bool(row[7]),
            customer_contacted=bool(row[8]),
            customer_marked_useful=bool(row[9]),
            customer_marked_bad=bool(row[10]),
            appointment=bool(row[11]),
            estimate_amount=row[12],
            won=bool(row[13]),
            won_amount=row[14],
            lost=bool(row[15]),
            disposition=row[16],
            reason_code=row[17],
            feedback_notes=row[18],
            created_at=row[19],
            updated_at=row[20],
        )
