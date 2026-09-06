"""
Incremental synchronization service for Miami-Dade Building Permits.
Pulls fresh and recently modified records starting from latest known ISSUDATE minus overlap window.
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


class SyncService:
    """Orchestrates incremental synchronization with safety overlap."""

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

    def run(self, overlap_minutes: Optional[int] = None) -> IngestionMetrics:
        """
        Executes incremental sync from latest known issued date minus overlap.
        """
        overlap = overlap_minutes if overlap_minutes is not None else config.incremental_overlap_minutes
        now_utc = datetime.now(timezone.utc)

        # 1. Determine baseline date
        latest_date_str = self.permit_store.get_latest_issued_date()
        if not latest_date_str:
            state = self.state_store.get_state()
            if state and state.get("last_processed_date"):
                latest_date_str = state["last_processed_date"]

        if latest_date_str:
            base_dt = datetime.fromisoformat(latest_date_str.replace("Z", "+00:00"))
            # Apply safety overlap
            sync_start_dt = base_dt - timedelta(minutes=overlap)
            logger.info(
                f"Resuming incremental sync from {base_dt.isoformat()} with {overlap}m overlap -> {sync_start_dt.isoformat()}"
            )
        else:
            # Fallback to backfill window if database is empty
            sync_start_dt = now_utc - timedelta(days=config.backfill_days)
            logger.info(f"No prior permit dates found. Starting incremental sync from {config.backfill_days} days ago.")

        start_str = sync_start_dt.strftime("%Y-%m-%d %H:%M:%S")
        where_clause = f"ISSUDATE >= TIMESTAMP '{start_str}'"

        logger.log_event(
            "job_started",
            job="sync",
            where=where_clause,
            overlap_minutes=overlap,
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
                    logger.info(f"Incremental sync reached end of updates at page {page_number}.")
                    break

                # Protect against infinite loops: if the FIRST record of this page
                # was already seen on a previous page, the server is ignoring
                # resultOffset — terminate regardless of page length.
                first_obj_id = (features[0].get("attributes") or {}).get("OBJECTID")
                if first_obj_id is not None and first_obj_id in seen_object_ids:
                    logger.warning(
                        f"Infinite loop protection triggered at offset {offset}: "
                        f"server re-returned OBJECTID {first_obj_id}. Terminating pagination."
                    )
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

                # Append raw records
                self.raw_store.insert_batch(raw_records)

                # Upsert normalized permits
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
                    f"Sync page {page_number}: received={len(features)}, new={new_cnt}, updated={updated_cnt}, dup={dup_cnt}, invalid={invalid_count}"
                )

                exceeded_transfer_limit = response.get("exceededTransferLimit", False)
                if not exceeded_transfer_limit and len(features) < page_size:
                    logger.info("Server indicated final sync page reached.")
                    break

                offset += len(features)
                page_number += 1

            final_metrics = tracker.finalize()
            newest_date = self.permit_store.get_latest_issued_date()

            self.state_store.update_checkpoint(
                job_id="miami_dade_arcgis",
                last_processed_date=newest_date or start_str,
                total_ingested=final_metrics.total_records,
                status="completed",
                metadata=final_metrics.to_dict(),
            )

            tracker.log_summary("sync")
            return final_metrics

        except Exception as e:
            logger.error(f"Incremental sync job failed: {e}", exc_info=True)
            self.state_store.update_checkpoint(
                job_id="miami_dade_arcgis",
                status="failed",
                metadata={"error": str(e)},
            )
            raise
