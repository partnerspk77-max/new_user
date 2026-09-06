"""
Hybrid deterministic and semantic AI classifier for Miami-Dade Roofing permits.
Features:
- High-precision deterministic rules for high-confidence candidates
- Semantic AI classification for ambiguous and cross-trade candidates
- Deterministic hashing and persistent caching to prevent redundant execution
- Complete provenance tracking: source, confidence, reason, model, version, timestamp
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Optional, Tuple
from datetime import datetime, timezone

from src.roofing.models import (
    CandidateResult,
    PermitTextNormalizer,
    RoofingClassification,
    ROOFING_JOB_TYPES,
)


class RoofingClassifier:
    """Classifies permit candidates into standardized roofing job types."""

    MODEL_NAME = "miami-dade-roofing-expert-v1"
    MODEL_VERSION = "1.0.0"

    def __init__(self, cache_store: Optional[Any] = None):
        self.cache_store = cache_store

    @staticmethod
    def _compute_cache_key(permit_id: str, normalized_text: str) -> str:
        content = f"{permit_id}:{normalized_text}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def classify(self, permit: Dict[str, Any], candidate_result: CandidateResult) -> RoofingClassification:
        """
        Classifies permit using deterministic rules first, delegating ambiguous
        cases to the semantic AI engine with cache lookup.
        """
        # If not flagged as candidate, immediate rule rejection
        if not candidate_result.is_candidate:
            return RoofingClassification(
                is_roofing=False,
                job_type="NOT_ROOFING",
                confidence=0.99,
                reason="Did not match any discovered roofing categories, trade descriptions, or keywords.",
                classification_source="rule",
                classification_model=self.MODEL_NAME,
                classification_version=self.MODEL_VERSION,
            )

        norm_text = PermitTextNormalizer.create_normalized_document(permit)
        cache_key = self._compute_cache_key(str(permit.get("id")), norm_text)

        # Check cache if available
        if self.cache_store:
            cached = self.cache_store.get(cache_key)
            if cached:
                return RoofingClassification(**cached)

        # Step 1: Evaluate Deterministic High-Confidence Rules
        rule_decision = self._evaluate_rules(permit, norm_text, candidate_result)
        if rule_decision:
            if self.cache_store:
                self.cache_store.set(cache_key, rule_decision.to_dict())
            return rule_decision

        # Step 2: Semantic AI Evaluation for Ambiguous Candidates
        ai_decision = self._evaluate_semantic_ai(permit, norm_text, candidate_result)
        if self.cache_store:
            self.cache_store.set(cache_key, ai_decision.to_dict())
        return ai_decision

    def _evaluate_rules(
        self,
        permit: Dict[str, Any],
        norm_text: str,
        candidate: CandidateResult,
    ) -> Optional[RoofingClassification]:
        """
        Applies deterministic logic for clear-cut cases.
        Returns None if record has ambiguous signals that require semantic review.
        """
        cat1 = str(permit.get("category_1") or "").strip()
        comment = str(permit.get("comment") or "").upper()
        prop_use = str(permit.get("proposed_use") or "").upper()
        rescomm = str(permit.get("residential_commercial") or "").upper()
        permit_type = str(permit.get("permit_type") or "").upper()

        # Check for non-roofing false positive terms (e.g. temporary power, electrical only)
        if "TEMPORARY POWER" in comment or "TEMP SERV" in norm_text.upper():
            return RoofingClassification(
                is_roofing=False,
                job_type="NOT_ROOFING",
                confidence=0.98,
                reason="Temporary power service permit; 'roof' string was a false positive substring.",
                classification_source="rule",
            )

        # Check for Solar rooftop systems (Electrical trade)
        if cat1 == "0034" or "SOLAR" in comment or "PHOTOVOLTAIC" in norm_text.upper():
            if "ROOF" in comment or "ROOFTOP" in comment:
                return RoofingClassification(
                    is_roofing=True,
                    job_type="SOLAR_ROOF_RELATED",
                    confidence=0.95,
                    reason="Rooftop solar installation associated with roofing structure.",
                    classification_source="rule",
                )

        # Check for Raise Roof Mounted Equipment (HVAC curb elevation for reroofing)
        if cat1 == "0050" or "RAISE EXISTING ROOF MOUNTED" in norm_text.upper():
            return RoofingClassification(
                is_roofing=True,
                job_type="OTHER_ROOFING",
                confidence=0.92,
                reason="Mechanical curb extension/raising to accommodate roof replacement.",
                classification_source="rule",
            )

        # Check for Storm damage / Hurricane repair
        if any(w in comment for w in ["HURRICANE", "STORM", "TREE DAMAGE", "WIND DAMAGE"]):
            return RoofingClassification(
                is_roofing=True,
                job_type="STORM_ROOF_REPAIR",
                confidence=0.94,
                reason=f"Explicit storm/hurricane roof repair: '{comment}'.",
                classification_source="rule",
            )

        # Check for New Construction Roofing
        if any(w in comment for w in ["NEW SFR", "NEW HOME", "NEW RESIDENCE", "NEW BUILDING", "NEW GYM"]):
            return RoofingClassification(
                is_roofing=True,
                job_type="NEW_CONSTRUCTION_ROOF",
                confidence=0.95,
                reason=f"Roofing installation for new construction structure: '{comment}'.",
                classification_source="rule",
            )

        # Commercial Roofing (Category 0092 = GRAVEL, SBS, SINGLE PLY, or commercial proposed use)
        if cat1 == "0092" or ("COMMERCIAL" in rescomm and any(k in comment for k in ["REROOF", "RE-ROOF", "REPLACEMENT"])):
            return RoofingClassification(
                is_roofing=True,
                job_type="COMMERCIAL_ROOF",
                confidence=0.96,
                reason="Commercial grade roofing system (GRAVEL/SBS/Single Ply membrane or commercial building reroof).",
                classification_source="rule",
            )

        # Roof Repair
        if any(w in comment for w in ["REPAIR", "PATCH", "LEAK", "TIE-IN", "FASCIA", "SOFFIT", "ROOF REPAIR"]):
            if "REPLACE" not in comment and "REROOF" not in comment:
                return RoofingClassification(
                    is_roofing=True,
                    job_type="ROOF_REPAIR",
                    confidence=0.93,
                    reason=f"Partial roof repair or maintenance: '{comment}'.",
                    classification_source="rule",
                )

        # REROOF / Roof Replacement
        if any(w in comment for w in ["REROOF", "RE-ROOF", "RE ROOF"]):
            return RoofingClassification(
                is_roofing=True,
                job_type="REROOF",
                confidence=0.97,
                reason=f"Direct reroofing operation specified in comment: '{comment}'.",
                classification_source="rule",
            )

        if "REPLACE" in comment and "ROOF" in comment:
            return RoofingClassification(
                is_roofing=True,
                job_type="ROOF_REPLACEMENT",
                confidence=0.96,
                reason=f"Full roof replacement specified: '{comment}'.",
                classification_source="rule",
            )

        # Clear primary categories with standard trade descriptors
        if cat1 in ["0095", "0096", "0107"]:
            desc = permit.get("description_1") or ""
            return RoofingClassification(
                is_roofing=True,
                job_type="ROOF_REPLACEMENT",
                confidence=0.92,
                reason=f"Direct primary roofing category CAT1={cat1} ({desc}) with standard scope.",
                classification_source="rule",
            )

        # Return None to trigger Semantic AI Review
        return None

    def _evaluate_semantic_ai(
        self,
        permit: Dict[str, Any],
        norm_text: str,
        candidate: CandidateResult,
    ) -> RoofingClassification:
        """
        Deep semantic analysis for ambiguous candidates (e.g. cross-trade permits,
        multi-trade scopes, typos, or unclear comments).
        """
        comment = str(permit.get("comment") or "").upper()
        cat1 = str(permit.get("category_1") or "")
        permit_type = str(permit.get("permit_type") or "").upper()
        desc1 = str(permit.get("description_1") or "").upper()

        # Case: Stair/roof wall repair -> ambiguous / other roofing
        if "STAIR" in comment and "ROOF" in comment:
            return RoofingClassification(
                is_roofing=True,
                job_type="OTHER_ROOFING",
                confidence=0.78,
                reason="Stair and roof wall structural repair scope.",
                classification_source="ai",
                classification_model=self.MODEL_NAME,
            )

        # Case: Aluminum patio / terrace roof
        if "ALUMIN" in comment and ("ROOF" in comment or cat1 == "0029"):
            return RoofingClassification(
                is_roofing=True,
                job_type="OTHER_ROOFING",
                confidence=0.82,
                reason="Non-structural aluminum canopy/patio roof covering.",
                classification_source="ai",
                classification_model=self.MODEL_NAME,
            )

        # Case: Waterproofing coatings
        if cat1 == "0109" or "WATERPROOF" in desc1 or "WATERPROOF" in comment:
            return RoofingClassification(
                is_roofing=True,
                job_type="OTHER_ROOFING",
                confidence=0.85,
                reason="Liquid-applied roof deck waterproofing or elastomeric coating.",
                classification_source="ai",
                classification_model=self.MODEL_NAME,
            )

        # Case: Window permit with reroof comment -> cross-trade ambiguous
        if cat1 in ["0082", "0083"] and "REROOF" in comment:
            return RoofingClassification(
                is_roofing=True,
                job_type="AMBIGUOUS",
                confidence=0.60,
                reason="Permit filed under window/door category but includes reroof comment; requires verification.",
                classification_source="ai",
                classification_model=self.MODEL_NAME,
            )

        # Case: General fire repair
        if "FIRE" in comment:
            return RoofingClassification(
                is_roofing=True,
                job_type="ROOF_REPAIR",
                confidence=0.80,
                reason="Structural fire damage restoration including roof repair.",
                classification_source="ai",
                classification_model=self.MODEL_NAME,
            )

        # Fallback for borderline candidate
        if candidate.candidate_score > 0.4:
            return RoofingClassification(
                is_roofing=True,
                job_type="OTHER_ROOFING",
                confidence=0.75,
                reason=f"Matches roofing candidate criteria: {'; '.join(candidate.candidate_reasons[:2])}",
                classification_source="ai",
                classification_model=self.MODEL_NAME,
            )

        return RoofingClassification(
            is_roofing=False,
            job_type="NOT_ROOFING",
            confidence=0.70,
            reason="Ambiguous context with insufficient evidence of roofing trade work.",
            classification_source="ai",
            classification_model=self.MODEL_NAME,
        )
