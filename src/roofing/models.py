"""
Domain models and text normalization for Miami-Dade Roofing Intelligence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


ROOFING_JOB_TYPES = [
    "ROOF_REPLACEMENT",
    "ROOF_REPAIR",
    "REROOF",
    "STORM_ROOF_REPAIR",
    "NEW_CONSTRUCTION_ROOF",
    "COMMERCIAL_ROOF",
    "SOLAR_ROOF_RELATED",
    "OTHER_ROOFING",
    "NOT_ROOFING",
    "AMBIGUOUS",
]


@dataclass
class CandidateResult:
    """Represents candidate detection output with audit reasons."""
    is_candidate: bool
    candidate_reasons: List[str] = field(default_factory=list)
    candidate_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_candidate": self.is_candidate,
            "candidate_reason": self.candidate_reasons,
            "candidate_score": self.candidate_score,
        }


@dataclass
class RoofingClassification:
    """Structured classification result with full audit provenance."""
    is_roofing: bool
    job_type: str
    confidence: float
    reason: str
    classification_source: str  # 'rule', 'ai', 'rule_and_ai', 'manual_review'
    classification_model: str = "miami-dade-roofing-classifier"
    classification_version: str = "1.0.0"
    classification_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PermitTextNormalizer:
    """
    Constructs normalized text representations for classification.
    Does NOT mutate original stored database values.
    """

    @staticmethod
    def clean_text(text: Optional[str]) -> str:
        if not text:
            return ""
        # Lowercase
        cleaned = text.lower().strip()
        # Normalize punctuation variations (replace slashes, hyphens, parens with spaces)
        cleaned = re.sub(r"[/\\_()\-]+", " ", cleaned)
        # Collapse repeated whitespace
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @classmethod
    def create_normalized_document(cls, permit: Dict[str, Any]) -> str:
        """
        Creates a structured, clean document text suitable for rule-matching and LLM prompts.
        """
        parts = []

        permit_type = cls.clean_text(permit.get("permit_type"))
        if permit_type:
            parts.append(f"permit_type: {permit_type}")

        # Categories and descriptions
        for i in range(1, 11):
            cat = cls.clean_text(permit.get(f"category_{i}"))
            desc = cls.clean_text(permit.get(f"description_{i}"))
            if cat or desc:
                parts.append(f"category_{i}: {cat} - {desc}")

        proposed_use = cls.clean_text(permit.get("proposed_use"))
        if proposed_use:
            parts.append(f"proposed_use: {proposed_use}")

        app_type = cls.clean_text(permit.get("application_type"))
        if app_type:
            parts.append(f"application_type: {app_type}")

        comment = cls.clean_text(permit.get("comment"))
        if comment:
            parts.append(f"comment: {comment}")

        contractor = cls.clean_text(permit.get("contractor_name"))
        if contractor:
            parts.append(f"contractor: {contractor}")

        rescomm = cls.clean_text(permit.get("residential_commercial"))
        if rescomm:
            parts.append(f"rescomm: {rescomm}")

        return "\n".join(parts)
