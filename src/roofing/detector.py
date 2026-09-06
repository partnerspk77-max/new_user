"""
Deterministic candidate detector for Miami-Dade Roofing permits.
Inspects permit_type, category codes, descriptions, proposed_use, and comments.
Produces audit reasons for why a record was flagged as a candidate.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from src.roofing.models import CandidateResult, PermitTextNormalizer


PRIMARY_ROOFING_CATEGORIES = {
    "0092": "Gravel, SBS, Single Ply Flat Roofing",
    "0095": "Asphalt / Fiberglass Shingle Roofing",
    "0096": "Metal & Wood Shingles/Shakes",
    "0107": "Tile Roofing",
}

SECONDARY_ROOFING_CATEGORIES = {
    "0109": "Waterproofing / Deck Coating",
    "0050": "Raise Existing Roof Mounted Equipment (HVAC)",
    "0106": "Light Weight Concrete Roof Deck",
}

# Strong direct roofing terms
EXPLICIT_ROOFING_TERMS = [
    r"\breroof\b",
    r"\bre-roof\b",
    r"\bre\s+roof\b",
    r"\broofing\b",
    r"\broof\b",
    r"\bshingle[s]?\b",
    r"\btile\s+roof\b",
    r"\bmetal\s+roof\b",
    r"\bflat\s+roof\b",
    r"\btpo\b",
    r"\bepdm\b",
    r"\bsbs\b",
    r"\btorch\s+down\b",
    r"\bmodified\s+bitumen\b",
    r"\broof\s+repair\b",
    r"\broof\s+replacement\b",
    r"\broof\s+covering\b",
    r"\broof\s+deck\b",
]

# Associated / Cross-trade roofing terms
ASSOCIATED_ROOFING_TERMS = [
    r"\broof\s+mount[a-z]*\b",
    r"\bsolar\b",
    r"\baluminum\s+roof\b",
    r"\bwaterproof[a-z]*\b",
    r"\bsoffit\b",
    r"\bfascia\b",
    r"\bflashing\b",
]


class RoofingCandidateDetector:
    """Detects potential roofing candidates using multi-layered deterministic rules."""

    def __init__(self):
        self._compiled_explicit = [re.compile(p, re.IGNORECASE) for p in EXPLICIT_ROOFING_TERMS]
        self._compiled_associated = [re.compile(p, re.IGNORECASE) for p in ASSOCIATED_ROOFING_TERMS]

    def evaluate(self, permit: Dict[str, Any]) -> CandidateResult:
        reasons: List[str] = []
        score = 0.0

        # 1. Check Category Codes (CAT1 - CAT10)
        found_primary_cat = False
        for i in range(1, 11):
            cat = str(permit.get(f"category_{i}") or "").strip()
            desc = str(permit.get(f"description_{i}") or "").strip()

            if cat in PRIMARY_ROOFING_CATEGORIES:
                reasons.append(f"CAT{i} matches primary roofing code {cat} ({PRIMARY_ROOFING_CATEGORIES[cat]})")
                score += 0.6
                found_primary_cat = True
            elif cat in SECONDARY_ROOFING_CATEGORIES:
                reasons.append(f"CAT{i} matches secondary roofing code {cat} ({SECONDARY_ROOFING_CATEGORIES[cat]})")
                score += 0.3

            # Check description text
            if desc:
                for regex in self._compiled_explicit:
                    if regex.search(desc):
                        reasons.append(f"DESC{i} '{desc}' contains explicit roofing terminology '{regex.pattern}'")
                        score += 0.4
                        break

        # 2. Check FFRMLINE (comment)
        comment = str(permit.get("comment") or "").strip()
        if comment:
            # Explicit terms
            for regex in self._compiled_explicit:
                if regex.search(comment):
                    reasons.append(f"Comment (FFRMLINE) contains explicit roofing term '{regex.pattern}'")
                    score += 0.5
                    break

            # Associated terms
            for regex in self._compiled_associated:
                if regex.search(comment):
                    reasons.append(f"Comment (FFRMLINE) contains associated roofing term '{regex.pattern}'")
                    score += 0.2
                    break

        # 3. Check Proposed Use & Application Type
        prop_use = str(permit.get("proposed_use") or "").strip()
        if prop_use and any(r.search(prop_use) for r in self._compiled_explicit):
            reasons.append(f"Proposed use '{prop_use}' contains roofing terminology")
            score += 0.2

        # 4. Check Contractor Name
        contractor = str(permit.get("contractor_name") or "").strip()
        if contractor and ("ROOF" in contractor.upper() or "SHINGLE" in contractor.upper()):
            reasons.append(f"Contractor name '{contractor}' indicates a dedicated roofing firm")
            score += 0.2

        is_candidate = score >= 0.3 or found_primary_cat

        return CandidateResult(
            is_candidate=is_candidate,
            candidate_reasons=reasons,
            candidate_score=min(round(score, 2), 1.0),
        )
