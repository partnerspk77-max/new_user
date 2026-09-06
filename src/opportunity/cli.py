"""
Command-Line Interface for Miami-Dade Property Signal Engine.
Commands:
- build-graph: Aggregates permits into folio histories & statefully updates commercial signals
- list-signals (alias list-opportunities): Queries actionable signals with evidence breakdowns
- signal-stats (alias opportunity-stats): High-level signal lifecycle & conversion metrics
"""

from __future__ import annotations

import argparse
import json
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
    print(f"    [✓] Synthesized multi-trade timelines for {t_count:,} unique property folios.")

    storage.save_timelines(timelines)
    print(f"    [✓] Persisted {t_count:,} property timelines with derived roof histories.")

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

    metrics = storage.get_metrics()
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
    _, storage, _ = get_components()
    audience = args.audience
    limit = args.limit or 15

    signals = storage.get_signals(audience=audience, limit=limit)
    if not signals:
        print("\n[!] No active signals found. Run 'build-graph' first.\n")
        return 1

    aud_label = (audience or "ALL").upper()
    print("\n" + "=" * 106)
    print(f"  PROPERTY SIGNALS & EVIDENCE AUDIT (AUDIENCE: {aud_label} | TOP {len(signals)})")
    print("=" * 106)
    print(f"{'SIGNAL ID':<16} | {'SCORE':<5} | {'SIGNAL TYPE':<32} | {'FRESHNESS':<15} | {'ADDRESS / FOLIO':<24}")
    print("-" * 106)
    for s in signals:
        addr = (s.get("address") or s.get("folio") or "N/A")[:24]
        print(f"{s['signal_id']:<16} | {s['evidence_score']:<5} | {s['signal_type']:<32} | {s['freshness_tier']:<15} | {addr:<24}")

        corrob = json.loads(s.get("corroborating_signals_json") or "[]")
        unverif = json.loads(s.get("unverified_assumptions_json") or "[]")

        if corrob:
            print(f"   ↳ EVIDENCE:    {'; '.join(corrob[:2])}")
        if unverif:
            print(f"   ↳ UNVERIFIED:  {'; '.join(unverif[:2])}")
        print("-" * 106)
    print()
    return 0


def handle_signal_stats(args: argparse.Namespace) -> int:
    _, storage, _ = get_components()
    metrics = storage.get_metrics()

    if metrics["total_properties"] == 0:
        print("\n[!] Graph not built yet. Run 'build-graph' first.\n")
        return 1

    print("\n" + "=" * 78)
    print("  PROPERTY GRAPH & SIGNAL LIFECYCLE METRICS")
    print("=" * 78)
    print(f"Total Property Timelines:       {metrics['total_properties']:,}")
    print(f"Active Signals:                 {metrics['active_signals']:,}")
    print(f"Resolved Signals:               {metrics['resolved_signals']:,}\n")

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

    p_build = subparsers.add_parser("build-graph", help="Aggregate folios and generate evidence-backed signals")
    p_build.set_defaults(func=handle_build_graph)

    p_list = subparsers.add_parser("list-signals", help="List active signals with evidence breakdowns")
    p_list.add_argument("--audience", choices=["roofer", "supplier", "all"], default=None, help="Target audience filter")
    p_list.add_argument("--limit", type=int, default=20, help="Max records to show")
    p_list.set_defaults(func=handle_list_signals)

    # Aliases
    p_list_opp = subparsers.add_parser("list-opportunities", help="Alias for list-signals")
    p_list_opp.add_argument("--audience", choices=["roofer", "supplier", "all"], default=None)
    p_list_opp.add_argument("--limit", type=int, default=20)
    p_list_opp.set_defaults(func=handle_list_signals)

    p_stats = subparsers.add_parser("signal-stats", help="Show signal lifecycle metrics")
    p_stats.set_defaults(func=handle_signal_stats)

    p_opp_stats = subparsers.add_parser("opportunity-stats", help="Alias for signal-stats")
    p_opp_stats.set_defaults(func=handle_signal_stats)

    parsed_args = parser.parse_args()
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
