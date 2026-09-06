"""
Command-Line Interface for Miami-Dade Permit Pipeline.
Callable by GitHub Actions, cron, workers, or manual developers:
- backfill: Pulls historical permits (90-180 days)
- sync: Performs incremental synchronization from latest timestamp with overlap
- inspect: Generates statistical discovery report on schema & categories
- export-json: Exports normalized permits to a JSON file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.client.arcgis_client import ArcGISClient
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

    print(f"[*] Starting historical backfill (target days: {args.days or config.backfill_days})...")
    metrics = service.run(
        days=args.days,
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

    overlap = args.overlap_minutes or config.incremental_overlap_minutes
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Miami-Dade County Building Permits Data Ingestion Pipeline (Phase 1)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. backfill
    p_backfill = subparsers.add_parser("backfill", help="Run historical permit backfill")
    p_backfill.add_argument("--days", type=int, help=f"Number of days to backfill (default: {config.backfill_days})")
    p_backfill.add_argument("--start-date", type=str, help="Explicit start date (YYYY-MM-DD)")
    p_backfill.add_argument("--end-date", type=str, help="Explicit end date (YYYY-MM-DD)")
    p_backfill.set_defaults(func=handle_backfill)

    # 2. sync
    p_sync = subparsers.add_parser("sync", help="Run incremental synchronization")
    p_sync.add_argument(
        "--overlap-minutes",
        type=int,
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
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
