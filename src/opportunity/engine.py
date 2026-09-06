"""
Property Opportunity & Signal Engine for Miami-Dade County.
Synthesizes multi-trade histories across all unique folios (8,362+ properties)
into transparent, evidence-backed commercial signals.
Adopts the Signal -> Evidence -> Opportunity -> Verified Lead mental model.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.opportunity.models import (
    PropertySignal,
    PropertyTimeline,
    SignalStatus,
    SignalType,
)
from src.storage.database import Database


class PropertyOpportunityEngine:
    """Processes parcel permit histories into multi-trade timelines and stateful commercial signals."""

    def __init__(self, db: Database, ref_dt: Optional[datetime] = None):
        self.db = db
        self.ref_dt = ref_dt or datetime.now(timezone.utc)

    def _calculate_freshness(self, issued_at: Optional[str]) -> Tuple[Optional[float], Optional[float], str]:
        if not issued_at:
            return None, None, "UNKNOWN"
        try:
            dt = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
            age_seconds = max(0.0, (self.ref_dt - dt).total_seconds())
            age_hours = round(age_seconds / 3600.0, 2)
            age_days = round(age_seconds / 86400.0, 2)
            if age_hours <= 24.0:
                tier = "NEW (0-24h)"
            elif age_hours <= 72.0:
                tier = "FRESH (1-3d)"
            elif age_hours <= 168.0:
                tier = "RECENT (4-7d)"
            elif age_hours <= 720.0:
                tier = "STALE (8-30d)"
            else:
                tier = "HISTORICAL (30d+)"
            return age_hours, age_days, tier
        except Exception:
            return None, None, "UNKNOWN"

    @staticmethod
    def _is_contractor_present(contractor: Optional[str]) -> bool:
        if not contractor:
            return False
        cleaned = contractor.strip().upper()
        if not cleaned or cleaned in ["NONE", "NULL", "UNASSIGNED", "OWNER", "OWNER-BUILDER", "OWNER / BUILDER"]:
            return False
        return True

    @staticmethod
    def _determine_roof_system(cat1: Optional[str], comment: str, desc1: str) -> Optional[str]:
        """Detects roofing material system from category codes and descriptions."""
        if cat1 == "0095" or "SHINGLE" in comment or "SHINGLE" in desc1:
            return "ASPHALT_SHINGLE"
        if cat1 == "0107" or "TILE" in comment or "TILE" in desc1:
            return "CONCRETE_OR_CLAY_TILE"
        if cat1 == "0096" or "METAL" in comment or "METAL" in desc1:
            return "METAL_OR_WOOD_SHAKE"
        if cat1 == "0092" or any(w in comment for w in ["GRAVEL", "SBS", "SINGLE PLY", "TPO", "EPDM", "MODIFIED"]):
            return "COMMERCIAL_MEMBRANE"
        if cat1 == "0109" or "WATERPROOF" in comment or "WATERPROOF" in desc1:
            return "WATERPROOFING_DECK_COATING"
        return "GENERAL_ROOF_COVERING"

    @staticmethod
    def _identify_renovation_signals(permit: Dict[str, Any]) -> List[str]:
        """Classifies the trade scope and specific modernization action."""
        signals = []
        ptype = str(permit.get("permit_type") or "").upper()
        cat1 = str(permit.get("category_1") or "")
        desc1 = str(permit.get("description_1") or "").upper()
        comment = str(permit.get("comment") or "").upper()

        if ptype == "MECH" or cat1 in ["0003", "0050"] or "A/C" in comment or "AIR COND" in desc1:
            signals.append("HVAC_AC_REPLACEMENT")
        if ptype == "ELEC":
            if cat1 == "0034" or "SOLAR" in comment:
                signals.append("SOLAR_PV_SYSTEM")
            else:
                signals.append("ELECTRICAL_UPGRADE")
        if ptype == "PLUM" or cat1 == "0001":
            signals.append("PLUMBING_REMODEL")
        if ptype == "BLDG":
            if cat1 in ["0082", "0083"] or "WINDOW" in comment or "DOOR" in comment or "SHUTTER" in comment:
                signals.append("WINDOW_DOOR_RETROFIT")
            elif any(w in comment for w in ["REMODEL", "ALTERATION", "ADDITION", "KITCHEN", "BATH"]):
                signals.append("INTERIOR_STRUCTURAL_REMODEL")
            elif "FENCE" in comment or cat1 == "0048":
                signals.append("PERIMETER_FENCE")
            elif "POOL" in comment or cat1 == "0032":
                signals.append("SWIMMING_POOL")
        return signals

    def build_property_timelines(self) -> Dict[str, PropertyTimeline]:
        """
        Groups all permits in the database by Folio to build rich property histories,
        deriving roof history, trade velocity, and modernization patterns.
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT p.*, r.roofing_job_type, r.roofing_confidence, r.classification_source
            FROM permits p
            LEFT JOIN roofing_permits r ON p.id = r.permit_id
            ORDER BY p.issued_at ASC, p.source_object_id ASC
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]

        # Group permits by Folio
        folio_groups = defaultdict(list)
        for r in rows:
            folio = r.get("folio")
            if folio and folio.strip():
                folio_groups[folio.strip()].append(r)

        timelines: Dict[str, PropertyTimeline] = {}

        for folio, permits in folio_groups.items():
            primary_address = next((p["address"] for p in reversed(permits) if p.get("address")), None)
            rescomm = next((p["residential_commercial"] for p in reversed(permits) if p.get("residential_commercial")), None)

            trades_set = set()
            active_trades_set = set()
            contractors_set = set()
            renovation_types_set = set()
            roof_permits_count = 0
            has_active_roof_permit = False
            last_roof_permit_date = None
            last_roof_system = None
            last_roof_contractor = None
            has_active_non_roof_permit = False

            first_date = permits[0].get("issued_at")
            latest_date = permits[-1].get("issued_at")

            for p in permits:
                ptype = p.get("permit_type")
                status = p.get("status")
                if ptype:
                    trades_set.add(ptype)
                    if status == "A":
                        active_trades_set.add(ptype)

                cname = p.get("contractor_name")
                if self._is_contractor_present(cname):
                    contractors_set.add(cname.strip())

                is_roofing = bool(p.get("roofing_job_type") and p.get("roofing_job_type") not in ["NOT_ROOFING", "SOLAR_ROOF_RELATED"])

                if is_roofing:
                    roof_permits_count += 1
                    last_roof_permit_date = p.get("issued_at")
                    cat1 = str(p.get("category_1") or "")
                    comment = str(p.get("comment") or "").upper()
                    desc1 = str(p.get("description_1") or "").upper()
                    last_roof_system = self._determine_roof_system(cat1, comment, desc1)
                    if self._is_contractor_present(cname):
                        last_roof_contractor = cname.strip()
                    if status == "A":
                        has_active_roof_permit = True
                else:
                    signals = self._identify_renovation_signals(p)
                    for s in signals:
                        renovation_types_set.add(s)
                    if status == "A":
                        has_active_non_roof_permit = True

            # Calculate years since last roof permit if one exists
            years_since_roof = None
            if last_roof_permit_date:
                try:
                    dt = datetime.fromisoformat(last_roof_permit_date.replace("Z", "+00:00"))
                    diff_days = max(0, (self.ref_dt - dt).days)
                    years_since_roof = round(diff_days / 365.25, 1)
                except Exception:
                    years_since_roof = None

            timelines[folio] = PropertyTimeline(
                folio=folio,
                address=primary_address,
                total_permits=len(permits),
                trades=sorted(list(trades_set)),
                active_trades=sorted(list(active_trades_set)),
                roof_permits_count=roof_permits_count,
                has_active_roof_permit=has_active_roof_permit,
                last_roof_permit_date=last_roof_permit_date,
                years_since_last_roof_permit=years_since_roof,
                last_roof_system=last_roof_system,
                last_roof_contractor=last_roof_contractor,
                has_active_non_roof_permit=has_active_non_roof_permit,
                non_roof_renovation_types=sorted(list(renovation_types_set)),
                contractors_seen=sorted(list(contractors_set)),
                residential_commercial=rescomm,
                first_permit_date=first_date,
                latest_permit_date=latest_date,
                permits=permits,
            )

        return timelines

    @staticmethod
    def _compute_signal_id(folio: str, signal_type: str, trade_scope: str) -> str:
        """Generates deterministic persistent identity for stateful lifecycle tracking."""
        key = f"{folio}:{signal_type}:{trade_scope}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()
        return f"SIG-{digest}"

    def generate_signals(
        self,
        timelines: Dict[str, PropertyTimeline],
    ) -> List[PropertySignal]:
        """
        Generates evidence-backed property signals using an additive, transparent scoring rubric.
        """
        signals: List[PropertySignal] = []

        for folio, timeline in timelines.items():
            # -------------------------------------------------------------
            # TRACK A: Permitted Projects (Suppliers & Canvassers)
            # Objective fact: a roofing permit exists and is active
            # -------------------------------------------------------------
            for p in timeline.permits:
                job_type = p.get("roofing_job_type")
                status = p.get("status")
                if job_type and job_type not in ["NOT_ROOFING", "SOLAR_ROOF_RELATED"] and status == "A":
                    issued_at = p.get("issued_at")
                    age_hours, age_days, freshness = self._calculate_freshness(issued_at)
                    cname = p.get("contractor_name")
                    contractor_present = self._is_contractor_present(cname)

                    # Fresh active roof permits are high-priority material supply dispatches
                    is_new = age_hours is not None and age_hours <= 168.0
                    sig_type = SignalType.NEW_ROOF_PERMIT if is_new else SignalType.ACTIVE_ROOF_PROJECT

                    score_breakdown = {
                        "base_permit_verification": 50.0,
                        "freshness_bonus": 35.0 if (age_hours is not None and age_hours <= 24.0) else (25.0 if (age_hours is not None and age_hours <= 72.0) else 15.0),
                        "contractor_verified": 10.0 if contractor_present else 0.0,
                    }
                    total_score = round(sum(score_breakdown.values()), 1)

                    corroborating = [
                        f"Active permitted roofing job under Category {p.get('category_1')} ({p.get('description_1')})",
                        f"Contractor on record: {cname if contractor_present else 'Unassigned / Owner-Builder'}",
                        f"Issued {int(age_hours or 0)} hours ago ({freshness})",
                    ]
                    unverified = [
                        "Job progress and tear-off schedule uninspected on site",
                        "Material supplier preference of contractor not yet confirmed",
                    ]

                    action = (
                        "Material dispatch: quote shingles/tile/membrane, delivery logistics, and neighbor canvassing"
                        if is_new
                        else "Job site intelligence: accessory supply, equipment rental, or sub-trade engagement"
                    )

                    signals.append(
                        PropertySignal(
                            signal_id=self._compute_signal_id(folio, sig_type, str(p.get("permit_number"))),
                            folio=folio,
                            address=p.get("address") or timeline.address,
                            signal_type=sig_type,
                            target_audience="SUPPLIER_DISTRIBUTOR",
                            status=SignalStatus.ACTIVE,
                            evidence_score=total_score,
                            evidence_breakdown=score_breakdown,
                            corroborating_signals=corroborating,
                            unverified_assumptions=unverified,
                            freshness_tier=freshness,
                            age_hours=age_hours,
                            age_days=age_days,
                            contractor_present=contractor_present,
                            contractor_name=cname if contractor_present else None,
                            trigger_trade="ROOF",
                            trigger_event=f"Active roofing permit ({p.get('permit_number')}) issued to {cname or 'Owner-Builder'}",
                            recommended_action=action,
                            residential_commercial=timeline.residential_commercial,
                            latitude=p.get("latitude"),
                            longitude=p.get("longitude"),
                        )
                    )

            # -------------------------------------------------------------
            # TRACK B: Modernization & Renovation Signals (Roofing Contractors)
            # Factual signals and hypotheses for uncontracted roof assessments
            # -------------------------------------------------------------

            # Signal B1: Owner-Builder Roof Filing (Uncontracted direct prospect)
            for p in timeline.permits:
                job_type = p.get("roofing_job_type")
                status = p.get("status")
                cname = p.get("contractor_name")
                if (
                    job_type
                    and job_type not in ["NOT_ROOFING", "SOLAR_ROOF_RELATED"]
                    and status == "A"
                    and not self._is_contractor_present(cname)
                ):
                    issued_at = p.get("issued_at")
                    age_hours, age_days, freshness = self._calculate_freshness(issued_at)

                    score_breakdown = {
                        "uncontracted_owner_filing": 50.0,
                        "freshness_bonus": 35.0 if (age_hours is not None and age_hours <= 72.0) else (20.0 if (age_hours is not None and age_hours <= 168.0) else 10.0),
                        "residential_scope": 10.0 if timeline.residential_commercial == "RESIDENTIAL" else 5.0,
                    }
                    total_score = round(sum(score_breakdown.values()), 1)

                    corroborating = [
                        "Homeowner pulled official roofing permit with NO licensed roofing contractor assigned",
                        f"Permit status Active, issued {int(age_hours or 0)} hours ago ({freshness})",
                    ]
                    unverified = [
                        "Owner may intend to self-perform labor under Florida owner-builder exemption statute 489.103(7)",
                        "Owner may have private unrecorded handshake agreement with an uncertified installer",
                    ]

                    signals.append(
                        PropertySignal(
                            signal_id=self._compute_signal_id(folio, SignalType.OWNER_BUILDER_ROOF_SIGNAL, str(p.get("permit_number"))),
                            folio=folio,
                            address=p.get("address") or timeline.address,
                            signal_type=SignalType.OWNER_BUILDER_ROOF_SIGNAL,
                            target_audience="ROOFING_CONTRACTOR",
                            status=SignalStatus.ACTIVE,
                            evidence_score=total_score,
                            evidence_breakdown=score_breakdown,
                            corroborating_signals=corroborating,
                            unverified_assumptions=unverified,
                            freshness_tier=freshness,
                            age_hours=age_hours,
                            age_days=age_days,
                            contractor_present=False,
                            contractor_name=None,
                            trigger_trade="ROOF",
                            trigger_event="Owner-builder pulled roof permit with NO licensed roofing contractor assigned",
                            recommended_action="Direct homeowner outreach: offer licensed contractor takeover to guarantee mandatory inspection pass",
                            residential_commercial=timeline.residential_commercial,
                            latitude=p.get("latitude"),
                            longitude=p.get("longitude"),
                        )
                    )

            # Signal B2: Multi-Trade Renovation Signal (Hypothesis: Heavy Capital Modernization with NO Roof Permit)
            if not timeline.has_active_roof_permit and timeline.has_active_non_roof_permit:
                reno_types = timeline.non_roof_renovation_types
                if reno_types:
                    active_non_roof = [
                        p for p in timeline.permits
                        if p.get("status") == "A" and not p.get("roofing_job_type")
                    ]
                    latest_p = active_non_roof[-1] if active_non_roof else timeline.permits[-1]
                    age_hours, age_days, freshness = self._calculate_freshness(latest_p.get("issued_at"))

                    # Transparent Additive Scoring Rubric (Max 100)
                    # 1. Renovation Velocity (0-35)
                    trade_count = len(timeline.active_trades)
                    if trade_count >= 3:
                        velocity_pts = 35.0
                    elif "HVAC_AC_REPLACEMENT" in reno_types and "WINDOW_DOOR_RETROFIT" in reno_types:
                        velocity_pts = 30.0
                    elif "HVAC_AC_REPLACEMENT" in reno_types or "WINDOW_DOOR_RETROFIT" in reno_types:
                        velocity_pts = 20.0
                    else:
                        velocity_pts = 15.0

                    # 2. Roof Permit History (0-35)
                    # CRITICAL: Missing evidence must NOT become positive evidence.
                    # 0 roof permits in a 180-day window is expected for most properties,
                    # not positive proof of an aging roof. Lifetime roof age requires Property Appraiser year_built.
                    if timeline.roof_permits_count == 0:
                        roof_history_pts = 0.0
                        roof_note = "No roof permit found in current dataset observation window"
                    elif timeline.years_since_last_roof_permit is not None:
                        if timeline.years_since_last_roof_permit >= 15.0:
                            roof_history_pts = 35.0
                            roof_note = f"Verified older roof: last recorded permit was {timeline.years_since_last_roof_permit} years ago"
                        elif timeline.years_since_last_roof_permit >= 10.0:
                            roof_history_pts = 20.0
                            roof_note = f"Aging roof: last recorded permit was {timeline.years_since_last_roof_permit} years ago"
                        else:
                            roof_history_pts = 0.0
                            roof_note = f"Recent roof on record ({timeline.years_since_last_roof_permit} yrs ago); replacement unlikely needed"
                    else:
                        roof_history_pts = 0.0
                        roof_note = "Historical roof permit date unverified in current dataset"

                    # 3. Freshness of Renovation Activity (0-20)
                    if age_hours is not None and age_hours <= 72.0:
                        freshness_pts = 20.0
                    elif age_hours is not None and age_hours <= 168.0:
                        freshness_pts = 12.0
                    elif age_hours is not None and age_hours <= 720.0:
                        freshness_pts = 5.0
                    else:
                        freshness_pts = 2.0

                    # 4. Property Scale (0-10)
                    scale_pts = 10.0 if timeline.residential_commercial == "RESIDENTIAL" else 5.0

                    score_breakdown = {
                        "renovation_velocity": velocity_pts,
                        "roof_history": roof_history_pts,
                        "activity_freshness": freshness_pts,
                        "property_scale": scale_pts,
                    }
                    total_score = round(min(100.0, sum(score_breakdown.values())), 1)

                    reno_str = ", ".join(reno_types[:3])
                    corroborating = [
                        f"Active multi-trade modernization: {reno_str}",
                        roof_note,
                        f"Latest permit activity issued {int(age_days or 0)} days ago ({freshness})",
                    ]
                    unverified = [
                        "Zero roof permits in dataset window does not confirm property has never had a roof replacement",
                        "Physical roof covering age and condition uninspected (requires Property Appraiser year_built or on-site assessment)",
                        "Owner may have replaced roof prior to dataset window or roof may still have remaining useful life",
                    ]

                    signals.append(
                        PropertySignal(
                            signal_id=self._compute_signal_id(folio, SignalType.PROPERTY_RENOVATION_SIGNAL, "MULTI_TRADE"),
                            folio=folio,
                            address=timeline.address,
                            signal_type=SignalType.PROPERTY_RENOVATION_SIGNAL,
                            target_audience="ROOFING_CONTRACTOR",
                            status=SignalStatus.ACTIVE,
                            evidence_score=total_score,
                            evidence_breakdown=score_breakdown,
                            corroborating_signals=corroborating,
                            unverified_assumptions=unverified,
                            freshness_tier=freshness,
                            age_hours=age_hours,
                            age_days=age_days,
                            contractor_present=True,
                            contractor_name=timeline.contractors_seen[-1] if timeline.contractors_seen else None,
                            trigger_trade="MULTI_TRADE",
                            trigger_event=f"Active modernization in progress ({reno_str}) with no active roof permit",
                            recommended_action="Pre-permit hypothesis outreach: property undergoing capital improvements; offer insurance roof inspection while trades are active",
                            residential_commercial=timeline.residential_commercial,
                            latitude=latest_p.get("latitude"),
                            longitude=latest_p.get("longitude"),
                        )
                    )

            # Signal B3: Solar Attachment Filing (Hypothesis: Roof assessment candidate prior to PV mounting)
            has_solar = any(
                p.get("category_1") == "0034" or "SOLAR" in str(p.get("comment") or "").upper()
                for p in timeline.permits
                if p.get("status") == "A"
            )
            if has_solar and timeline.roof_permits_count == 0:
                solar_permit = next(
                    p for p in timeline.permits
                    if (p.get("category_1") == "0034" or "SOLAR" in str(p.get("comment") or "").upper()) and p.get("status") == "A"
                )
                age_hours, age_days, freshness = self._calculate_freshness(solar_permit.get("issued_at"))

                score_breakdown = {
                    "solar_attachment_filing": 45.0,
                    "no_roof_permit_on_record": 25.0,
                    "freshness_bonus": 15.0 if (age_hours is not None and age_hours <= 168.0) else 5.0,
                }
                total_score = round(sum(score_breakdown.values()), 1)

                corroborating = [
                    "Active rooftop solar PV permit filed on property",
                    "No roof permit recorded in county dataset",
                    f"Filing freshness: {freshness}",
                ]
                unverified = [
                    "Existing roof may already be structurally sound or newer than dataset window",
                    "Specific utility/jurisdiction reroof requirement unverified for this installation",
                ]

                signals.append(
                    PropertySignal(
                        signal_id=self._compute_signal_id(folio, SignalType.SOLAR_ROOF_SIGNAL, str(solar_permit.get("permit_number"))),
                        folio=folio,
                        address=timeline.address,
                        signal_type=SignalType.SOLAR_ROOF_SIGNAL,
                        target_audience="ROOFING_CONTRACTOR",
                        status=SignalStatus.ACTIVE,
                        evidence_score=total_score,
                        evidence_breakdown=score_breakdown,
                        corroborating_signals=corroborating,
                        unverified_assumptions=unverified,
                        freshness_tier=freshness,
                        age_hours=age_hours,
                        age_days=age_days,
                        contractor_present=self._is_contractor_present(solar_permit.get("contractor_name")),
                        contractor_name=solar_permit.get("contractor_name"),
                        trigger_trade="ELEC",
                        trigger_event="Active Solar PV permit filed with no roof permit on record",
                        recommended_action="Pre-solar roof inspection outreach: offer roof certification prior to solar panel installation",
                        residential_commercial=timeline.residential_commercial,
                        latitude=solar_permit.get("latitude"),
                        longitude=solar_permit.get("longitude"),
                    )
                )

        return signals

    def generate_opportunities(
        self,
        timelines: Dict[str, PropertyTimeline],
    ) -> List[PropertySignal]:
        """Backward compatibility alias for generate_signals."""
        return self.generate_signals(timelines)
