"""
Historical backfill orchestrator for Miami-Dade Building Permits.
Ingests 90-180 days of historical data using deterministic date ordering and offset pagination.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from src.client.arcgis_client import ArcGISClient
from src.client.exceptions import ValidationError
from src.config import config
from src.logger import logger
from src.models.permit import IngestionMetrics, NormalizedPermit, RawRecord
from src.pipeline.metrics import MetricsTracker
from src.pipeline.normalizer import PermitNormalizer
from src.storage.permit_store import PermitStore
from src.storage.raw_store import RawStore
from src.storage.state_store import StateStore


class BackfillService:
    """Coordinates historical permit extraction and persistence."""

    def __init__(
        self,
        client: ArcGISClient,
        raw_store: RawStore,
        permit_store: PermitStore,
        state_store: StateStore,
        normalizer: PermitNormalizer = PermitNormalizer(),
    ):
        self.client = client
        self.raw_store = raw_store
        self.permit_store = permit_store
        self.state_store = state_store
        self.normalizer = normalizer

    def run(
        self,
        days: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> IngestionMetrics:
        """
        Executes historical backfill.
        If start_date is not provided, calculates (now - days) as start window.
        """
        target_days = days or config.backfill_days
        now_utc = datetime.now(timezone.utc)

        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        else:
            start_dt = now_utc - timedelta(days=target_days)

        start_str = start_dt.strftime("%Y-%m-%d 00:00:00")
        where_clause = f"ISSUDATE >= TIMESTAMP '{start_str}'"
        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            end_str = end_dt.strftime("%Y-%m-%d 23:59:59")
            where_clause += f" AND ISSUDATE <= TIMESTAMP '{end_str}'"

        logger.log_event(
            "job_started",
            job="backfill",
            where=where_clause,
            target_days=target_days,
            start_date=start_str,
        )

        tracker = MetricsTracker()
        offset = 0
        page_number = 1
        page_size = self.client.page_size
        seen_object_ids = set()

        try:
            while True:
                response = self.client.query_records(
                    where=where_clause,
                    offset=offset,
                    limit=page_size,
                    order_by="ISSUDATE ASC, OBJECTID ASC",
                    page_number=page_number,
                )

                features = response.get("features", [])
                if not features:
                    logger.info(f"Backfill reached end of dataset at page {page_number}, offset {offset}.")
                    break

                # Protect against infinite loop: verify new records are being returned
                first_obj_id = (features[0].get("attributes") or {}).get("OBJECTID")
                if first_obj_id in seen_object_ids and len(features) == 1:
                    logger.warning(f"Infinite loop protection triggered at offset {offset}. Terminating.")
                    break
                for f in features:
                    oid = (f.get("attributes") or {}).get("OBJECTID")
                    if oid:
                        seen_object_ids.add(oid)

                raw_records: list[RawRecord] = []
                normalized_permits: list[NormalizedPermit] = []
                invalid_count = 0

                fetch_time = datetime.now(timezone.utc).isoformat()

                for f in features:
                    try:
                        raw_rec, norm_permit = self.normalizer.normalize_feature(f, fetched_at=fetch_time)
                        raw_records.append(raw_rec)
                        normalized_permits.append(norm_permit)
                    except ValidationError as ve:
                        logger.warning(f"Validation error on feature: {ve}")
                        invalid_count += 1
                    except Exception as ex:
                        logger.error(f"Unexpected normalization error: {ex}", exc_info=True)
                        invalid_count += 1

                # Persist raw records first
                self.raw_store.insert_batch(raw_records)

                # Persist normalized permits with deterministic upsert
                new_cnt, updated_cnt, dup_cnt = self.permit_store.upsert_batch(normalized_permits)

                # Track metrics
                tracker.record_page_results(
                    received_count=len(features),
                    new_count=new_cnt,
                    updated_count=updated_cnt,
                    duplicate_count=dup_cnt,
                    invalid_count=invalid_count,
                    permits=normalized_permits,
                )

                logger.info(
                    f"Page {page_number} processed: received={len(features)}, new={new_cnt}, updated={updated_cnt}, dup={dup_cnt}, invalid={invalid_count}"
                )

                # Check if server indicated end of dataset
                exceeded_transfer_limit = response.get("exceededTransferLimit", False)
                if not exceeded_transfer_limit and len(features) < page_size:
                    logger.info("Server indicated final page reached.")
                    break

                offset += len(features)
                page_number += 1

            # Update checkpoint state
            final_metrics = tracker.finalize()
            latest_db_date = self.permit_store.get_latest_issued_date()
            self.state_store.update_checkpoint(
                job_id="miami_dade_arcgis",
                last_processed_date=latest_db_date or start_str,
                total_ingested=final_metrics.total_records,
                status="completed",
                metadata=final_metrics.to_dict(),
            )

            tracker.log_summary("backfill")
            return final_metrics

        except Exception as e:
            logger.error(f"Backfill job failed: {e}", exc_info=True)
            self.state_store.update_checkpoint(
                job_id="miami_dade_arcgis",
                status="failed",
                metadata={"error": str(e)},
            )
            raise
