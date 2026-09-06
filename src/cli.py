"""
Command-Line Interface for Miami-Dade Permit Pipeline.
Callable by GitHub Actions, cron, workers, or manual developers:
- backfill: Pulls historical permits (90-180 days)
- sync: Performs incremental synchronization from latest timestamp with overlap
- inspect: Generates statistical discovery report on schema & categories
- export-json: Exports normalized permits to a JSON file
- run-all: ONE-SHOT end-to-end production run (ingest -> classify -> enrich -> signals -> feeds)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from src.client.arcgis_client import ArcGISClient
from src.client.exceptions import ArcGISError, ParcelFetchError, WeatherFetchError
from src.config import config
from src.discovery.inspector import PermitInspector
from src.logger import logger
from src.pipeline.backfill import BackfillService
from src.pipeline.sync import SyncService
from src.storage.database import Database
from src.storage.permit_store import PermitStore
from src.storage.raw_store import RawStore
from src.storage.state_store import StateStore


def init_pipeline():
    """Initializes and wires pipeline dependencies (Dependency Injection)."""
    db = Database(config.database_url)
    db.initialize_schema()

    client = ArcGISClient()
    raw_store = RawStore(db)
    permit_store = PermitStore(db)
    state_store = StateStore(db)

    return db, client, raw_store, permit_store, state_store


def handle_backfill(args: argparse.Namespace) -> int:
    db, client, raw_store, permit_store, state_store = init_pipeline()
    service = BackfillService(client, raw_store, permit_store, state_store)

    days = args.days if args.days is not None else config.backfill_days
    print(f"[*] Starting historical backfill (target days: {days})...")
    metrics = service.run(
        days=days,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    print("\n[✓] Historical Backfill Completed Successfully!")
    print(f"    Total Processed:  {metrics.total_records:,}")
    print(f"    New Permits:      {metrics.new_records:,}")
    print(f"    Updated Permits:  {metrics.updated_records:,}")
    print(f"    Duplicate Permits:{metrics.duplicate_records:,}")
    print(f"    Invalid Records:  {metrics.invalid_records:,}")
    print(f"    Issue Date Range: {metrics.min_issue_date} to {metrics.max_issue_date}")
    print(f"    Duration:         {metrics.duration_seconds}s\n")
    return 0


def handle_sync(args: argparse.Namespace) -> int:
    db, client, raw_store, permit_store, state_store = init_pipeline()
    service = SyncService(client, raw_store, permit_store, state_store)

    overlap = args.overlap_minutes if args.overlap_minutes is not None else config.incremental_overlap_minutes
    print(f"[*] Starting incremental synchronization (overlap: {overlap} min)...")
    metrics = service.run(overlap_minutes=overlap)

    print("\n[✓] Incremental Sync Completed Successfully!")
    print(f"    Total Processed:  {metrics.total_records:,}")
    print(f"    New Permits:      {metrics.new_records:,}")
    print(f"    Updated Permits:  {metrics.updated_records:,}")
    print(f"    Duplicates/Skipped:{metrics.duplicate_records:,}")
    print(f"    Invalid Records:  {metrics.invalid_records:,}")
    print(f"    Latest Date:      {metrics.max_issue_date}")
    print(f"    Duration:         {metrics.duration_seconds}s\n")
    return 0


def handle_inspect(args: argparse.Namespace) -> int:
    db, client, _, _, _ = init_pipeline()
    inspector = PermitInspector(db, client)

    stats = inspector.inspect_local()
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        inspector.print_report(stats)
    return 0


def handle_export_json(args: argparse.Namespace) -> int:
    db, _, _, permit_store, _ = init_pipeline()
    out_path = Path(args.output)
    count = permit_store.export_to_json(out_path)
    print(f"[✓] Exported {count:,} normalized permits to {out_path}")
    return 0


# ---------------------------------------------------------------------------
# ONE-SHOT END-TO-END ORCHESTRATION
# ---------------------------------------------------------------------------

def _print_stage(n: int, total: int, title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  STAGE {n}/{total}: {title}")
    print("=" * 78)


def handle_run_all(args: argparse.Namespace) -> int:
    """
    Executes the full lead-generation pipeline end to end:

        1. Ingest       (backfill if DB empty, else incremental sync)
        2. Classify     (roofing detection & classification)
        3. Enrich       (parcel attributes + NOAA storm reports — network, degradable)
        4. Build Graph  (property timelines & evidence-backed signals)
        5. Export Feeds (supplier / roofer / market-intelligence feeds)
        6. Report       (audit + lifecycle metrics)

    Network-dependent stages degrade gracefully: a failure there is reported
    but never blocks the local pipeline or the export of already-cached data.
    """
    from src.opportunity import cli as opp_cli
    from src.roofing import cli as roof_cli

    total_stages = 6
    failed_stages: list[str] = []
    skipped_stages: list[str] = []
    start_time = time.perf_counter()

    print("\n" + "#" * 78)
    print("#  MIAMI-DADE LEAD GENERATION PIPELINE — FULL END-TO-END RUN")
    print("#" * 78)

    # ---- Stage 1: Ingestion (backfill vs sync decided automatically) ----
    _print_stage(1, total_stages, "PERMIT INGESTION (auto: backfill if empty, else incremental sync)")
    db, _, _, permit_store, _ = init_pipeline()
    row_count = permit_store.count()
    try:
        if row_count == 0 or args.force_backfill:
            ns = argparse.Namespace(
                days=args.days,
                start_date=args.start_date or None,
                end_date=args.end_date or None,
            )
            print(f"[i] Database holds {row_count:,} permits -> running historical backfill.")
            rc = handle_backfill(ns)
        else:
            ns = argparse.Namespace(overlap_minutes=args.overlap_minutes)
            print(f"[i] Database holds {row_count:,} permits -> running incremental sync.")
            rc = handle_sync(ns)
        if rc != 0:
            failed_stages.append("ingestion")
    except (ArcGISError, Exception) as exc:
        logger.error(f"Ingestion stage failed: {exc}", exc_info=True)
        print(f"\n[!] INGESTION FAILED: {exc}")
        if row_count == 0:
            print("[!] Database is empty — downstream stages will have nothing to process.")
            return 1
        print("[i] Continuing with the {0:,} permits already in the database.".format(row_count))
        failed_stages.append("ingestion")

    # ---- Stage 2: Roofing classification ----
    _print_stage(2, total_stages, "ROOFING CLASSIFICATION")
    try:
        rc = roof_cli.handle_classify_roofing(argparse.Namespace())
        if rc != 0:
            failed_stages.append("classification")
    except Exception as exc:
        logger.error(f"Classification stage failed: {exc}", exc_info=True)
        print(f"\n[!] CLASSIFICATION FAILED: {exc}")
        failed_stages.append("classification")

    # ---- Stage 3: Enrichment (network-dependent, degradable) ----
    if args.skip_enrichment:
        skipped_stages.append("parcel enrichment")
        skipped_stages.append("storm enrichment")
        print("\n[i] Skipping enrichment stages (--skip-enrichment).")
    else:
        _print_stage(3, total_stages, "ENRICHMENT (parcels + NOAA storms)")
        try:
            ns = argparse.Namespace(batch_size=100, limit=args.enrich_limit, force=False)
            rc = opp_cli.handle_enrich_parcels(ns)
            if rc != 0:
                failed_stages.append("parcel enrichment")
        except (ParcelFetchError, Exception) as exc:
            logger.warning(f"Parcel enrichment degraded: {exc}")
            print(f"\n[!] PARCEL ENRICHMENT UNAVAILABLE THIS RUN: {exc}")
            print("[i] Continuing — cached parcel data is still used where available.")
            failed_stages.append("parcel enrichment")

        try:
            ns = argparse.Namespace(start_date=args.storm_start_date)
            rc = opp_cli.handle_enrich_weather(ns)
            if rc != 0:
                failed_stages.append("storm enrichment")
        except (WeatherFetchError, Exception) as exc:
            logger.warning(f"Storm enrichment degraded: {exc}")
            print(f"\n[!] STORM ENRICHMENT UNAVAILABLE THIS RUN: {exc}")
            print("[i] Continuing — cached storm data is still used where available.")
            failed_stages.append("storm enrichment")

    # ---- Stage 4: Property graph & signals ----
    _print_stage(4, total_stages, "PROPERTY GRAPH & SIGNAL SYNTHESIS")
    try:
        rc = opp_cli.handle_build_graph(argparse.Namespace())
        if rc != 0:
            failed_stages.append("graph building")
    except Exception as exc:
        logger.error(f"Graph stage failed: {exc}", exc_info=True)
        print(f"\n[!] GRAPH BUILD FAILED: {exc}")
        failed_stages.append("graph building")
        print("[!] Cannot export feeds without signals. Aborting remaining stages.")
        _print_run_summary(failed_stages, skipped_stages, start_time)
        return 1

    # ---- Stage 5: Commercial feed exports ----
    _print_stage(5, total_stages, "COMMERCIAL FEED EXPORT")
    try:
        rc = opp_cli.handle_export_feeds(argparse.Namespace())
        if rc != 0:
            failed_stages.append("feed export")
    except Exception as exc:
        logger.error(f"Feed export failed: {exc}", exc_info=True)
        print(f"\n[!] FEED EXPORT FAILED: {exc}")
        failed_stages.append("feed export")

    # ---- Stage 6: Audit & lifecycle report ----
    _print_stage(6, total_stages, "AUDIT & SIGNAL METRICS")
    try:
        rc = roof_cli.handle_audit_roofing(argparse.Namespace())
        if rc != 0:
            failed_stages.append("audit")
    except Exception as exc:
        logger.warning(f"Audit stage degraded: {exc}")
        failed_stages.append("audit")

    try:
        opp_cli.handle_signal_stats(argparse.Namespace())
    except Exception as exc:
        logger.warning(f"Signal stats degraded: {exc}")

    _print_run_summary(failed_stages, skipped_stages, start_time)
    return 1 if ("ingestion" in failed_stages or "graph building" in failed_stages) else 0


def _print_run_summary(failed_stages: list[str], skipped_stages: list[str], start_time: float) -> None:
    duration = round(time.perf_counter() - start_time, 1)
    print("\n" + "#" * 78)
    print("#  END-TO-END RUN SUMMARY")
    print("#" * 78)
    print(f"    Total Duration: {duration}s")
    if failed_stages:
        print(f"    Degraded Stages (pipeline still produced output): {', '.join(failed_stages)}")
    if skipped_stages:
        print(f"    Skipped Stages: {', '.join(skipped_stages)}")
    if not failed_stages and not skipped_stages:
        print("    All stages completed successfully.")
    print("    Deliverables are in the data/ directory:")
    print("      - feed_permitted_projects_suppliers.csv     (Feed 1: suppliers)")
    print("      - feed_pre_permit_roof_opportunities.csv    (Feed 2: roofers)")
    print("      - feed_contractor_market_intelligence.csv   (Feed 3: market intel)")
    print("      - property_signals_roofers.csv / _suppliers.csv (unified signals)")
    print("#" * 78 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="permit-pipeline",
        description="Miami-Dade County Building Permits Lead Generation Pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 0. run-all (the one-shot production command)
    p_all = subparsers.add_parser(
        "run-all",
        help="ONE-SHOT end-to-end run: ingest -> classify -> enrich -> signals -> feeds",
    )
    p_all.add_argument("--days", type=int, default=None, help="Backfill window in days (first run only)")
    p_all.add_argument("--start-date", type=str, default="", help="Explicit backfill start date (YYYY-MM-DD)")
    p_all.add_argument("--end-date", type=str, default="", help="Explicit backfill end date (YYYY-MM-DD)")
    p_all.add_argument("--overlap-minutes", type=int, default=None, help="Overlap window for incremental sync")
    p_all.add_argument("--force-backfill", action="store_true", help="Force backfill even if DB already has permits")
    p_all.add_argument("--skip-enrichment", action="store_true", help="Skip network enrichment (parcels/storms)")
    p_all.add_argument("--enrich-limit", type=int, default=0, help="Max folios to enrich this run (0 = all)")
    p_all.add_argument("--storm-start-date", type=str, default="2024-01-01T00:00Z", help="Storm reports start date")
    p_all.set_defaults(func=handle_run_all)

    # 1. backfill
    p_backfill = subparsers.add_parser("backfill", help="Run historical permit backfill")
    p_backfill.add_argument("--days", type=int, default=None, help=f"Number of days to backfill (default: {config.backfill_days})")
    p_backfill.add_argument("--start-date", type=str, default=None, help="Explicit start date (YYYY-MM-DD)")
    p_backfill.add_argument("--end-date", type=str, default=None, help="Explicit end date (YYYY-MM-DD)")
    p_backfill.set_defaults(func=handle_backfill)

    # 2. sync
    p_sync = subparsers.add_parser("sync", help="Run incremental synchronization")
    p_sync.add_argument(
        "--overlap-minutes",
        type=int,
        default=None,
        help=f"Overlap window in minutes (default: {config.incremental_overlap_minutes})",
    )
    p_sync.set_defaults(func=handle_sync)

    # 3. inspect
    p_inspect = subparsers.add_parser("inspect", help="Run discovery analysis & statistics")
    p_inspect.add_argument("--json", action="store_true", help="Output raw JSON instead of human-readable report")
    p_inspect.set_defaults(func=handle_inspect)

    # 4. export-json
    p_export = subparsers.add_parser("export-json", help="Export normalized database to JSON")
    p_export.add_argument("--output", type=str, default="data/permits.json", help="Output file path")
    p_export.set_defaults(func=handle_export_json)

    parsed_args = parser.parse_args()
    try:
        return parsed_args.func(parsed_args)
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user. Checkpoint saved — re-run the same command to resume.")
        return 130
    except Exception as exc:
        # Friendly top-level error: full detail goes to the log file, one clear
        # line goes to the operator.
        logger.error(f"Command '{parsed_args.command}' failed: {exc}", exc_info=True)
        print(f"\n[!] COMMAND FAILED: {exc}")
        print(f"    Stage: {parsed_args.command}")
        print(f"    Full traceback written to logs/. Fix the cause and re-run — "
              f"all commands are idempotent and safe to retry.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
