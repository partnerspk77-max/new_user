"""
Metrics aggregation and reporting for permit ingestion runs.
"""

from __future__ import annotations

import time
from typing import Any, Dict
from src.logger import logger
from src.models.permit import IngestionMetrics, NormalizedPermit


class MetricsTracker:
    """Collects and summarizes execution metrics for Phase 1 pipeline."""

    def __init__(self):
        self.metrics = IngestionMetrics()
        self._start_time = time.perf_counter()

    def record_page_results(
        self,
        received_count: int,
        new_count: int,
        updated_count: int,
        duplicate_count: int,
        invalid_count: int,
        permits: list[NormalizedPermit],
    ) -> None:
        self.metrics.total_records += received_count
        self.metrics.new_records += new_count
        self.metrics.updated_records += updated_count
        self.metrics.duplicate_records += duplicate_count
        self.metrics.invalid_records += invalid_count

        for p in permits:
            if p.folio:
                self.metrics.records_with_folio += 1
            if p.contractor_name or p.contractor_number:
                self.metrics.records_with_contractor += 1
            if p.latitude is not None and p.longitude is not None:
                self.metrics.records_with_geometry += 1
            self.metrics.update_issue_date(p.issued_at)

    def finalize(self) -> IngestionMetrics:
        self.metrics.duration_seconds = round(time.perf_counter() - self._start_time, 2)
        return self.metrics

    def log_summary(self, job_name: str) -> None:
        m = self.finalize()
        logger.log_event(
            "job_completed",
            job=job_name,
            total_records=m.total_records,
            new_records=m.new_records,
            updated_records=m.updated_records,
            duplicate_records=m.duplicate_records,
            invalid_records=m.invalid_records,
            records_with_folio=m.records_with_folio,
            records_with_contractor=m.records_with_contractor,
            records_with_geometry=m.records_with_geometry,
            min_issue_date=m.min_issue_date,
            max_issue_date=m.max_issue_date,
            duration_seconds=m.duration_seconds,
        )
