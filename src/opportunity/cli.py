"""
Command-Line Interface for Miami-Dade Property Signal Engine.
Commands:
- build-graph: Aggregates permits into folio histories, joins parcel intelligence, & statefully updates commercial signals
- enrich-parcels: Fetches physical building characteristics from Miami-Dade Property Appraiser (PaGISView)
- list-signals (alias list-opportunities): Queries actionable signals with evidence breakdowns and parcel attributes
- signal-stats (alias opportunity-stats): High-level signal lifecycle & conversion metrics
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from src.client.parcel_client import ParcelClient
from src.config import config
from src.enrichment.storage import ParcelStorage
from src.opportunity.engine import PropertyOpportunityEngine
from src.opportunity.storage import OpportunityStorage
from src.storage.database import Database


def get_components():
    db = Database(config.database_url)
    db.initialize_schema()
    storage = OpportunityStorage(db)
    parcel_storage = ParcelStorage(db)
    engine = PropertyOpportunityEngine(db)
    return db, storage, parcel_storage, engine


def handle_enrich_parcels(args: argparse.Namespace) -> int:
    db, _, parcel_storage, _ = get_components()
    batch_size = args.batch_size or 100
    limit = args.limit or 0
    force = args.force

    print("\n" + "=" * 78)
    print("  MIAMI-DADE PROPERTY APPRAISER (PAPA) PARCEL ENRICHMENT")
    print("=" * 78)

    # 1. Fetch all unique folios from permits database
    conn = db.get_connection()
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT folio FROM permits WHERE folio IS NOT NULL AND folio != ''")
        all_folios = [r[0].replace("-", "").strip() for r in cur.fetchall() if r[0]]

    total_unique = len(all_folios)
    print(f"Total Unique Folios in Permit Database: {total_unique:,}")

    if not force:
        target_folios = parcel_storage.get_unenriched_folios(all_folios)
        print(f"Already Cached in property_parcels:      {total_unique - len(target_folios):,}")
        print(f"Unenriched Folios Remaining:             {len(target_folios):,}")
    else:
        target_folios = all_folios
        print(f"Force re-querying all {len(target_folios):,} folios...")

    if not target_folios:
        print("\n[✓] All property folios are already enriched! No network requests needed.")
        print("    Run 'build-graph' to synthesize parcel attributes into signals.\n")
        return 0

    if limit > 0:
        target_folios = target_folios[:limit]
        print(f"Applying limit: fetching {len(target_folios):,} folios in this run.")

    # 2. Ingest via ParcelClient
    client = ParcelClient(batch_size=batch_size)
    start_time = time.perf_counter()

    print(f"\n[*] Querying Miami-Dade ArcGIS PaGISView in batches of {batch_size}...")

    def on_progress(fetched: int, total: int):
        pct = round((fetched / total) * 100, 1)
        sys.stdout.write(f"\r    [→] Enriched {fetched:,} / {total:,} parcels ({pct}%)")
        sys.stdout.flush()

    parcels = client.fetch_all_parcels(target_folios, progress_callback=on_progress)
    print()

    # 3. Upsert into database
    saved = parcel_storage.upsert_parcels(parcels)
    duration = round(time.perf_counter() - start_time, 2)

    stats = parcel_storage.get_stats()
    coverage_pct = round((stats["total_parcels"] / total_unique) * 100, 1) if total_unique else 0

    print("\n" + "=" * 78)
    print("  PARCEL ENRICHMENT COMPLETE")
    print("=" * 78)
    print(f"Parcels Retrieved / Updated:       {saved:,}")
    print(f"Total Parcels in Cache:            {stats['total_parcels']:,} of {total_unique:,} ({coverage_pct}% coverage)")
    print(f"Parcels with Verified Year Built:  {stats['parcels_with_year_built']:,}")
    print(f"Parcels with Building Square Feet: {stats['parcels_with_area']:,}")
    print(f"Parcels with Identified Owner:     {stats['parcels_with_owner']:,}")
    if stats["avg_year_built"]:
        print(f"Average Structure Construction Yr: {stats['avg_year_built']}")
    print(f"Enrichment Duration:               {duration}s")
    print("=" * 78)
    print("\n[✓] Next step: Run 'python -m src.opportunity.cli build-graph' to correlate building age.")
    return 0


def handle_build_graph(args: argparse.Namespace) -> int:
    db, storage, parcel_storage, engine = get_components()

    print("\n[*] Starting Property Graph Aggregation across all permits...")
    start_time = time.perf_counter()

    # Fetch cached parcel data to join physical attributes
    parcels_map = parcel_storage.get_all_parcels_map()
    p_count = len(parcels_map)
    print(f"    [✓] Loaded {p_count:,} enriched parcel profiles from local cache.")

    timelines = engine.build_property_timelines(parcels_map=parcels_map)
    t_count = len(timelines)
    print(f"    [✓] Synthesized multi-trade timelines for {t_count:,} unique property folios.")

    storage.save_timelines(timelines)
    print(f"    [✓] Persisted {t_count:,} property timelines with derived roof & building histories.")

    print("\n[*] Detecting commercial signals (Track A: Permitted Projects & Track B: Modernization Signals)...")
    signals = engine.generate_signals(timelines)
    s_count = len(signals)
    print(f"    [✓] Generated {s_count:,} discrete commercial signals.")

    # Stateful upsert: preserves first_detected_at, updates last_seen_at, marks missing as RESOLVED
    storage.save_signals(signals)
    print(f"    [✓] Statefully synced signals to property_signals table (0 dropped records).")

    # Export primary signal feeds
    csv_roofers = Path("data/property_signals_roofers.csv")
    csv_suppliers = Path("data/property_signals_suppliers.csv")
    json_all = Path("data/property_signals.json")

    export_stats = storage.export_signals(csv_roofers, csv_suppliers, json_all)

    # Also maintain backward-compatible files
    legacy_roofers = Path("data/opportunities_roofers.csv")
    legacy_suppliers = Path("data/opportunities_suppliers.csv")
    legacy_json = Path("data/commercial_opportunities.json")
    storage.export_signals(legacy_roofers, legacy_suppliers, legacy_json)

    metrics = storage.get_stats()
    duration = round(time.perf_counter() - start_time, 2)

    print("\n" + "=" * 78)
    print("  MIAMI-DADE PROPERTY SIGNAL ENGINE: BUILD COMPLETE")
    print("=" * 78)
    print(f"Unique Property Parcels (Folios):  {metrics['total_properties']:,}")
    print(f"Active Commercial Signals:         {metrics['active_signals']:,}")
    print(f"Resolved / Closed Signals:         {metrics['resolved_signals']:,}")
    print(f"Processing Duration:               {duration}s\n")

    print("--- [1] Value Streams Breakdown ---")
    for aud, count in sorted(metrics["by_audience"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["active_signals"]) * 100, 1) if metrics["active_signals"] else 0
        name = "Track B: Modernization Signals (Pre-Permit Roofer Hypotheses)" if "ROOF" in aud else "Track A: Permitted Projects (Material Supply & Logistics)"
        print(f"  {name:<65}: {count:>5,} ({pct:>5.1f}%)")
    print()

    print("--- [2] Signal Taxonomy ---")
    for s_type, count in sorted(metrics["by_type"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["active_signals"]) * 100, 1) if metrics["active_signals"] else 0
        print(f"  {s_type:<35}: {count:>5,} ({pct:>5.1f}%)")
    print()

    print("--- [3] Freshness Distribution ---")
    for f_tier, count in sorted(metrics["by_freshness"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["active_signals"]) * 100, 1) if metrics["active_signals"] else 0
        print(f"  {f_tier:<25}: {count:>5,} ({pct:>5.1f}%)")
    print("=" * 78)

    print(f"\n[✓] Exported signal feeds:")
    print(f"    - Roofer Signals:   {csv_roofers} ({export_stats['roofers']:,} records)")
    print(f"    - Supplier Feeds:   {csv_suppliers} ({export_stats['suppliers']:,} records)")
    print(f"    - Unified Pipeline: {json_all} ({export_stats['total']:,} records)\n")
    return 0


def handle_list_signals(args: argparse.Namespace) -> int:
    _, storage, _, _ = get_components()
    audience = args.audience
    limit = args.limit or 15

    signals = storage.get_signals(audience=audience, limit=limit)
    if not signals:
        print("\n[!] No active signals found. Run 'build-graph' first.\n")
        return 1

    aud_label = (audience or "ALL").upper()
    print("\n" + "=" * 118)
    print(f"  PROPERTY SIGNALS & EVIDENCE AUDIT (AUDIENCE: {aud_label} | TOP {len(signals)})")
    print("=" * 118)
    print(f"{'SIGNAL ID':<16} | {'SCORE':<5} | {'SIGNAL TYPE':<28} | {'AGE / YR':<10} | {'SQFT':<8} | {'ADDRESS / OWNER':<36}")
    print("-" * 118)
    for s in signals:
        addr = (s.get("address") or s.get("folio") or "N/A")[:36]
        yr = str(s.get("year_built") or "—")
        b_age = f"{s.get('building_age')}y" if s.get("building_age") is not None else "—"
        yr_label = f"{yr} ({b_age})" if yr != "—" else "—"
        sqft = f"{int(s.get('building_actual_area')):,}" if s.get("building_actual_area") else "—"

        print(f"{s['signal_id']:<16} | {s['evidence_score']:<5} | {s['signal_type']:<28} | {yr_label:<10} | {sqft:<8} | {addr:<36}")

        owner = s.get("owner_name")
        if owner:
            print(f"   ↳ OWNER:       {owner[:50]}")

        corrob = json.loads(s.get("corroborating_signals_json") or "[]")
        unverif = json.loads(s.get("unverified_assumptions_json") or "[]")

        if corrob:
            print(f"   ↳ EVIDENCE:    {'; '.join(corrob[:2])}")
        if unverif:
            print(f"   ↳ UNVERIFIED:  {'; '.join(unverif[:2])}")
        print("-" * 118)
    print()
    return 0


def handle_signal_stats(args: argparse.Namespace) -> int:
    _, storage, parcel_storage, _ = get_components()
    metrics = storage.get_stats()
    parcel_stats = parcel_storage.get_stats()

    if metrics["total_properties"] == 0:
        print("\n[!] Graph not built yet. Run 'build-graph' first.\n")
        return 1

    print("\n" + "=" * 78)
    print("  PROPERTY GRAPH & SIGNAL LIFECYCLE METRICS")
    print("=" * 78)
    print(f"Total Property Timelines:       {metrics['total_properties']:,}")
    print(f"Active Signals:                 {metrics['active_signals']:,}")
    print(f"Resolved Signals:               {metrics['resolved_signals']:,}")
    print(f"Enriched Parcels in Cache:      {parcel_stats['total_parcels']:,}")
    print(f"Parcels with Verified Year:     {parcel_stats['parcels_with_year_built']:,}\n")

    print("--- By Target Audience ---")
    for aud, count in metrics["by_audience"].items():
        print(f"  {aud:<30}: {count:>5,}")
    print()

    print("--- By Signal Type ---")
    for st, count in metrics["by_type"].items():
        print(f"  {st:<35}: {count:>5,}")
    print()

    print("--- By Freshness Tier ---")
    for ft, count in metrics["by_freshness"].items():
        print(f"  {ft:<25}: {count:>5,}")
    print("=" * 78 + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Miami-Dade Property Signal Engine CLI"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # enrich-parcels
    p_enrich = subparsers.add_parser("enrich-parcels", help="Fetch building characteristics from Property Appraiser GIS")
    p_enrich.add_argument("--batch-size", type=int, default=100, help="ArcGIS batch chunk size (default: 100)")
    p_enrich.add_argument("--limit", type=int, default=0, help="Max folios to enrich (0 = all unenriched)")
    p_enrich.add_argument("--force", action="store_true", help="Re-fetch all folios even if cached")
    p_enrich.set_defaults(func=handle_enrich_parcels)

    # build-graph
    p_build = subparsers.add_parser("build-graph", help="Aggregate folios and generate evidence-backed signals")
    p_build.set_defaults(func=handle_build_graph)

    # list-signals
    p_list = subparsers.add_parser("list-signals", help="List active signals with evidence breakdowns")
    p_list.add_argument("--audience", choices=["roofer", "supplier", "all"], default=None, help="Target audience filter")
    p_list.add_argument("--limit", type=int, default=20, help="Max records to show")
    p_list.set_defaults(func=handle_list_signals)

    # Aliases
    p_list_opp = subparsers.add_parser("list-opportunities", help="Alias for list-signals")
    p_list_opp.add_argument("--audience", choices=["roofer", "supplier", "all"], default=None)
    p_list_opp.add_argument("--limit", type=int, default=20)
    p_list_opp.set_defaults(func=handle_list_signals)

    # signal-stats
    p_stats = subparsers.add_parser("signal-stats", help="Show signal lifecycle metrics")
    p_stats.set_defaults(func=handle_signal_stats)

    p_opp_stats = subparsers.add_parser("opportunity-stats", help="Alias for signal-stats")
    p_opp_stats.set_defaults(func=handle_signal_stats)

    parsed_args = parser.parse_args()
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
