"""
Configuration management for Miami-Dade Permit Pipeline.
Follows 12-Factor App design with environment variable overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env if present
load_dotenv()


@dataclass(frozen=True)
class PipelineConfig:
    """Immutable pipeline configuration."""

    api_url: str = field(
        default_factory=lambda: os.getenv(
            "MIAMI_DADE_PERMITS_URL",
            "https://gisweb.miamidade.gov/arcgis/rest/services/MD_LandInformation/MapServer/1/query",
        )
    )
    page_size: int = field(
        default_factory=lambda: int(os.getenv("PERMIT_PAGE_SIZE", "1000"))
    )
    request_timeout: int = field(
        default_factory=lambda: int(os.getenv("PERMIT_REQUEST_TIMEOUT", "30"))
    )
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("PERMIT_MAX_RETRIES", "5"))
    )
    retry_backoff_factor: float = field(
        default_factory=lambda: float(os.getenv("PERMIT_RETRY_BACKOFF_FACTOR", "1.5"))
    )
    request_delay_seconds: float = field(
        default_factory=lambda: float(os.getenv("PERMIT_REQUEST_DELAY_SECONDS", "0.2"))
    )
    backfill_days: int = field(
        default_factory=lambda: int(os.getenv("PERMIT_BACKFILL_DAYS", "90"))
    )
    incremental_overlap_minutes: int = field(
        default_factory=lambda: int(os.getenv("PERMIT_INCREMENTAL_OVERLAP_MINUTES", "120"))
    )
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///data/permits.db")
    )
    log_dir: Path = field(
        default_factory=lambda: Path(os.getenv("LOG_DIR", "logs"))
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "USER_AGENT",
            "MiamiDadePermitIngestionPipeline/1.0 (+https://github.com/balmen/permit-pipeline)",
        )
    )
    api_token: Optional[str] = field(
        default_factory=lambda: os.getenv("ARCGIS_API_TOKEN")
    )


# Singleton config instance
config = PipelineConfig()
