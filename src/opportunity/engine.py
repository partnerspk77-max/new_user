"""
Property Opportunity Engine for Miami-Dade County.
Analyzes multi-trade timelines across all unique folios (8,362+ properties)
to generate high-precision opportunities for:
- Track A: Project Intelligence (Material Suppliers, Distributors, Canvassers)
- Track B: Pre-Permit Opportunities (Roofing Contractors seeking uncontracted owners)
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.opportunity.models import CommercialOpportunity, PropertyTimeline
from src.storage.database import Database


class PropertyOpportunityEngine:
    """Processes parcel permit histories into multi-trade timelines and actionable leads."""

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
        Groups all permits in the database by Folio to build rich property histories.
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        # Query all permits, joined with roofing classification if available
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
            contractors_set = set()
            renovation_types_set = set()
            roof_permits_count = 0
            has_active_roof_permit = False
            last_roof_permit_date = None
            has_active_non_roof_permit = False

            first_date = permits[0].get("issued_at")
            latest_date = permits[-1].get("issued_at")

            for p in permits:
                ptype = p.get("permit_type")
                if ptype:
                    trades_set.add(ptype)

                cname = p.get("contractor_name")
                if self._is_contractor_present(cname):
                    contractors_set.add(cname.strip())

                is_roofing = bool(p.get("roofing_job_type") and p.get("roofing_job_type") not in ["NOT_ROOFING", "SOLAR_ROOF_RELATED"])
                status = p.get("status")

                if is_roofing:
                    roof_permits_count += 1
                    last_roof_permit_date = p.get("issued_at")
                    if status == "A":
                        has_active_roof_permit = True
                else:
                    signals = self._identify_renovation_signals(p)
                    for s in signals:
                        renovation_types_set.add(s)
                    if status == "A":
                        has_active_non_roof_permit = True

            timelines[folio] = PropertyTimeline(
                folio=folio,
                address=primary_address,
                total_permits=len(permits),
                trades=sorted(list(trades_set)),
                roof_permits_count=roof_permits_count,
                has_active_roof_permit=has_active_roof_permit,
                last_roof_permit_date=last_roof_permit_date,
                has_active_non_roof_permit=has_active_non_roof_permit,
                non_roof_renovation_types=sorted(list(renovation_types_set)),
                contractors_seen=sorted(list(contractors_set)),
                residential_commercial=rescomm,
                first_permit_date=first_date,
                latest_permit_date=latest_date,
                permits=permits,
            )

        return timelines

    def generate_opportunities(
        self,
        timelines: Dict[str, PropertyTimeline],
    ) -> List[CommercialOpportunity]:
        """
        Generates actionable commercial opportunities for both Track A and Track B.
        """
        opportunities: List[CommercialOpportunity] = []
        opp_id_counter = 1

        for folio, timeline in timelines.items():
            # -------------------------------------------------------------
            # TRACK A: Project Intelligence for Material Suppliers & Distributors
            # Targets properties with active roofing permits
            # -------------------------------------------------------------
            for p in timeline.permits:
                job_type = p.get("roofing_job_type")
                status = p.get("status")
                if job_type and job_type not in ["NOT_ROOFING", "SOLAR_ROOF_RELATED"] and status == "A":
                    issued_at = p.get("issued_at")
                    age_hours, age_days, freshness = self._calculate_freshness(issued_at)
                    cname = p.get("contractor_name")
                    contractor_present = self._is_contractor_present(cname)

                    # Fresh active roof permits are prime distributor leads
                    if age_hours is not None and age_hours <= 168.0:
                        opp_type = "NEW_ROOF_PERMIT"
                        base_priority = 85.0
                        if age_hours <= 24.0:
                            priority = base_priority + 15.0
                        elif age_hours <= 72.0:
                            priority = base_priority + 10.0
                        else:
                            priority = base_priority + 5.0

                        event = f"Brand new roofing permit issued ({int(age_hours)}h ago) to {cname or 'Owner-Builder'}"
                        action = "Supply dispatch: quote shingles/tile/membrane, delivery logistics, and neighbor canvassing"
                    else:
                        opp_type = "ACTIVE_ROOF_PROJECT"
                        priority = 65.0
                        event = f"Active roofing job site permitted {int(age_days or 0)} days ago"
                        action = "Job site intelligence: accessory supply, equipment rental, or sub-trade engagement"

                    opportunities.append(
                        CommercialOpportunity(
                            opportunity_id=f"OPP-SUP-{opp_id_counter:06d}",
                            folio=folio,
                            address=p.get("address") or timeline.address,
                            opportunity_type=opp_type,
                            target_audience="SUPPLIER_DISTRIBUTOR",
                            priority_score=round(priority, 1),
                            freshness_tier=freshness,
                            age_hours=age_hours,
                            age_days=age_days,
                            contractor_present=contractor_present,
                            contractor_name=cname if contractor_present else None,
                            trigger_trade="ROOF",
                            trigger_event=event,
                            recommended_action=action,
                            residential_commercial=timeline.residential_commercial,
                            latitude=p.get("latitude"),
                            longitude=p.get("longitude"),
                        )
                    )
                    opp_id_counter += 1

            # -------------------------------------------------------------
            # TRACK B: Pre-Permit & Uncontracted Opportunities for Roofing Contractors
            # -------------------------------------------------------------
            
            # Opportunity B1: Unassigned / Owner-Builder Active Roof Permits
            # Direct prospect where homeowner has NO licensed roofer on file!
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
                    priority = 95.0 if (age_hours is not None and age_hours <= 168.0) else 80.0

                    opportunities.append(
                        CommercialOpportunity(
                            opportunity_id=f"OPP-ROOF-{opp_id_counter:06d}",
                            folio=folio,
                            address=p.get("address") or timeline.address,
                            opportunity_type="UNASSIGNED_ROOF_PERMIT",
                            target_audience="ROOFING_CONTRACTOR",
                            priority_score=round(priority, 1),
                            freshness_tier=freshness,
                            age_hours=age_hours,
                            age_days=age_days,
                            contractor_present=False,
                            contractor_name=None,
                            trigger_trade="ROOF",
                            trigger_event="Owner-builder pulled roof permit with NO licensed roofing contractor assigned",
                            recommended_action="Direct homeowner outreach: offer licensed contractor takeover to pass mandatory inspections",
                            residential_commercial=timeline.residential_commercial,
                            latitude=p.get("latitude"),
                            longitude=p.get("longitude"),
                        )
                    )
                    opp_id_counter += 1

            # Opportunity B2: Major Renovation with NO Roof Permit (Pre-Permit Roofer Prospect)
            # Property has active major renovations (AC, impact windows, repiping, alterations)
            # but ZERO roofing permits on file!
            if not timeline.has_active_roof_permit and timeline.has_active_non_roof_permit:
                reno_types = timeline.non_roof_renovation_types
                if reno_types:
                    # Find latest active non-roof permit for freshness calculation
                    active_non_roof = [
                        p for p in timeline.permits
                        if p.get("status") == "A" and not p.get("roofing_job_type")
                    ]
                    latest_p = active_non_roof[-1] if active_non_roof else timeline.permits[-1]
                    age_hours, age_days, freshness = self._calculate_freshness(latest_p.get("issued_at"))

                    # Multi-trade velocity calculation
                    trade_count = len(timeline.trades)
                    if trade_count >= 3:
                        priority = 88.0
                    elif "HVAC_AC_REPLACEMENT" in reno_types or "WINDOW_DOOR_RETROFIT" in reno_types:
                        priority = 82.0
                    else:
                        priority = 74.0

                    # Adjust for freshness
                    if age_hours is not None and age_hours <= 72.0:
                        priority += 8.0
                    elif age_hours is not None and age_hours <= 168.0:
                        priority += 4.0

                    reno_str = ", ".join(reno_types[:3])
                    event_desc = f"Active modernization in progress ({reno_str}) with NO roof permit on record"
                    action_desc = "Pre-permit sales outreach: owner is investing heavily in property; pitch insurance roof upgrade while trades are active"

                    opportunities.append(
                        CommercialOpportunity(
                            opportunity_id=f"OPP-ROOF-{opp_id_counter:06d}",
                            folio=folio,
                            address=timeline.address,
                            opportunity_type="PROPERTY_RENOVATION_OPPORTUNITY",
                            target_audience="ROOFING_CONTRACTOR",
                            priority_score=round(min(98.0, priority), 1),
                            freshness_tier=freshness,
                            age_hours=age_hours,
                            age_days=age_days,
                            contractor_present=True,
                            contractor_name=timeline.contractors_seen[-1] if timeline.contractors_seen else None,
                            trigger_trade="MULTI_TRADE",
                            trigger_event=event_desc,
                            recommended_action=action_desc,
                            residential_commercial=timeline.residential_commercial,
                            latitude=latest_p.get("latitude"),
                            longitude=latest_p.get("longitude"),
                        )
                    )
                    opp_id_counter += 1

            # Opportunity B3: Active Solar PV Installation with NO Recent Roof Permit
            # In Florida, solar installations often mandate reroofing if the roof is >8 years old
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

                opportunities.append(
                    CommercialOpportunity(
                        opportunity_id=f"OPP-ROOF-{opp_id_counter:06d}",
                        folio=folio,
                        address=timeline.address,
                        opportunity_type="SOLAR_REROOF_OPPORTUNITY",
                        target_audience="ROOFING_CONTRACTOR",
                        priority_score=91.0,
                        freshness_tier=freshness,
                        age_hours=age_hours,
                        age_days=age_days,
                        contractor_present=self._is_contractor_present(solar_permit.get("contractor_name")),
                        contractor_name=solar_permit.get("contractor_name"),
                        trigger_trade="ELEC",
                        trigger_event="Active Solar PV permit filed with no roof permit on file; roof replacement or certification needed prior to solar installation",
                        recommended_action="Offer pre-solar roof certification and reroofing to prevent future solar array detach & reset costs",
                        residential_commercial=timeline.residential_commercial,
                        latitude=solar_permit.get("latitude"),
                        longitude=solar_permit.get("longitude"),
                    )
                )
                opp_id_counter += 1

        return opportunities
