"""
Command-Line Interface for Miami-Dade Property Opportunity Engine.
Commands:
- build-graph: Aggregates permits into 8,362+ folio timelines & generates dual-track opportunities
- list-opportunities: Queries actionable leads for roofers or suppliers
- opportunity-stats: High-level pipeline conversion & property velocity metrics
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from src.config import config
from src.opportunity.engine import PropertyOpportunityEngine
from src.opportunity.storage import OpportunityStorage
from src.storage.database import Database


def get_components():
    db = Database(config.database_url)
    db.initialize_schema()
    storage = OpportunityStorage(db)
    engine = PropertyOpportunityEngine(db)
    return db, storage, engine


def handle_build_graph(args: argparse.Namespace) -> int:
    db, storage, engine = get_components()

    print("\n[*] Starting Property Graph Aggregation across all permits...")
    start_time = time.perf_counter()

    timelines = engine.build_property_timelines()
    t_count = len(timelines)
    print(f"    [✓] Built multi-trade timelines for {t_count:,} unique property folios.")

    storage.save_timelines(timelines)
    print(f"    [✓] Persisted {t_count:,} property timelines to database.")

    print("\n[*] Generating commercial opportunities (Track A: Suppliers & Track B: Roofers)...")
    opportunities = engine.generate_opportunities(timelines)
    o_count = len(opportunities)
    print(f"    [✓] Identified {o_count:,} actionable commercial opportunities.")

    storage.save_opportunities(opportunities)
    print(f"    [✓] Saved {o_count:,} opportunities to commercial_opportunities table.")

    # Export datasets
    csv_roofers = Path("data/opportunities_roofers.csv")
    csv_suppliers = Path("data/opportunities_suppliers.csv")
    json_all = Path("data/commercial_opportunities.json")

    export_stats = storage.export_opportunities(csv_roofers, csv_suppliers, json_all)
    metrics = storage.get_metrics()
    duration = round(time.perf_counter() - start_time, 2)

    print("\n" + "=" * 78)
    print("  MIAMI-DADE PROPERTY OPPORTUNITY ENGINE: BUILD COMPLETE")
    print("=" * 78)
    print(f"Unique Property Parcels (Folios):  {metrics['total_properties']:,}")
    print(f"Total Actionable Opportunities:    {metrics['total_opportunities']:,}")
    print(f"Processing Duration:               {duration}s\n")

    print("--- [1] Value Streams Breakdown ---")
    for aud, count in sorted(metrics["by_audience"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["total_opportunities"]) * 100, 1)
        name = "Track B: Roofing Contractors (Pre-Permit & Unassigned)" if "ROOF" in aud else "Track A: Material Suppliers & Distributors (Permitted Jobs)"
        print(f"  {name:<60}: {count:>5,} ({pct:>5.1f}%)")
    print()

    print("--- [2] Opportunity Types ---")
    for o_type, count in sorted(metrics["by_type"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["total_opportunities"]) * 100, 1)
        print(f"  {o_type:<35}: {count:>5,} ({pct:>5.1f}%)")
    print()

    print("--- [3] Freshness Distribution ---")
    for f_tier, count in sorted(metrics["by_freshness"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["total_opportunities"]) * 100, 1)
        print(f"  {f_tier:<25}: {count:>5,} ({pct:>5.1f}%)")
    print("=" * 78)

    print(f"\n[✓] Exported clean segmented feeds:")
    print(f"    - Roofer Leads:   {csv_roofers} ({export_stats['roofers']:,} records)")
    print(f"    - Supplier Leads: {csv_suppliers} ({export_stats['suppliers']:,} records)")
    print(f"    - Full Pipeline:  {json_all} ({export_stats['total']:,} records)\n")
    return 0


def handle_list_opportunities(args: argparse.Namespace) -> int:
    _, storage, _ = get_components()
    audience = args.audience
    limit = args.limit or 25

    opps = storage.get_opportunities(audience=audience, limit=limit)
    if not opps:
        print("\n[!] No opportunities found. Run 'build-graph' first.\n")
        return 1

    aud_label = (audience or "ALL").upper()
    print("\n" + "=" * 100)
    print(f"  ACTIONABLE OPPORTUNITY PIPELINE (AUDIENCE: {aud_label} | TOP {len(opps)})")
    print("=" * 100)
    print(f"{'OPP ID':<16} | {'SCORE':<5} | {'TYPE':<30} | {'FRESHNESS':<15} | {'ADDRESS / FOLIO':<24}")
    print("-" * 100)
    for o in opps:
        addr = (o.get("address") or o.get("folio") or "N/A")[:24]
        print(f"{o['opportunity_id']:<16} | {o['priority_score']:<5} | {o['opportunity_type']:<30} | {o['freshness_tier']:<15} | {addr:<24}")
        print(f"   ↳ TRIGGER: {o['trigger_event'][:92]}")
        print(f"   ↳ ACTION:  {o['recommended_action'][:92]}")
        print("-" * 100)
    print()
    return 0


def handle_opportunity_stats(args: argparse.Namespace) -> int:
    _, storage, _ = get_components()
    metrics = storage.get_metrics()

    if metrics["total_properties"] == 0:
        print("\n[!] Graph not built yet. Run 'build-graph' first.\n")
        return 1

    print("\n" + "=" * 78)
    print("  PROPERTY GRAPH & COMMERCIAL METRICS")
    print("=" * 78)
    print(f"Total Property Timelines:       {metrics['total_properties']:,}")
    print(f"Total Generated Opportunities:  {metrics['total_opportunities']:,}\n")

    print("--- By Target Audience ---")
    for aud, count in metrics["by_audience"].items():
        print(f"  {aud:<30}: {count:>5,}")
    print()

    print("--- By Opportunity Type ---")
    for ot, count in metrics["by_type"].items():
        print(f"  {ot:<35}: {count:>5,}")
    print()

    print("--- By Freshness Tier ---")
    for ft, count in metrics["by_freshness"].items():
        print(f"  {ft:<25}: {count:>5,}")
    print("=" * 78 + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Miami-Dade Property Opportunity Engine CLI"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_build = subparsers.add_parser("build-graph", help="Aggregate folios and generate opportunities")
    p_build.set_defaults(func=handle_build_graph)

    p_list = subparsers.add_parser("list-opportunities", help="List actionable leads")
    p_list.add_argument("--audience", choices=["roofer", "supplier", "all"], default=None, help="Target audience filter")
    p_list.add_argument("--limit", type=int, default=20, help="Max records to show")
    p_list.set_defaults(func=handle_list_opportunities)

    p_stats = subparsers.add_parser("opportunity-stats", help="Show pipeline metrics")
    p_stats.set_defaults(func=handle_opportunity_stats)

    parsed_args = parser.parse_args()
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
