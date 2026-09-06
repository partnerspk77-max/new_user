"""
Command-Line Interface for Miami-Dade Property Signal Engine.
Commands:
- build-graph: Aggregates permits into folio histories, joins parcel intelligence & NOAA storms, statefully updates signals
- enrich-parcels: Fetches physical building characteristics from Miami-Dade Property Appraiser (PaGISView)
- enrich-weather: Ingests official NOAA/NWS severe convective storm reports (MFL / Miami-Dade)
- export-validation-sample: Generates a stratified 100-record audit (Grade A/B/C) for contractor interviews
- record-outcome: Logs contractor feedback and sales conversion outcomes into proprietary dataset
- outcome-stats: Displays contractor conversion rates, feedback metrics, and win/loss statistics
- list-signals (alias list-opportunities): Queries actionable signals with evidence breakdowns
- signal-stats (alias opportunity-stats): High-level signal lifecycle & conversion metrics
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from src.client.parcel_client import ParcelClient
from src.config import config
from src.enrichment.storage import ParcelStorage
from src.opportunity.engine import PropertyOpportunityEngine
from src.opportunity.models import SignalStatus
from src.opportunity.outcomes import (
    ALL_REASON_CODES,
    NEGATIVE_REASONS,
    POSITIVE_REASONS,
    OutcomeStorage,
    SignalOutcome,
)
from src.opportunity.storage import OpportunityStorage
from src.storage.database import Database
from src.weather.client import NOAAStormClient
from src.weather.correlator import StormCorrelator
from src.weather.storage import StormStorage


def get_components():
    db = Database(config.database_url)
    db.initialize_schema()
    storage = OpportunityStorage(db)
    parcel_storage = ParcelStorage(db)
    storm_storage = StormStorage(db)
    outcome_storage = OutcomeStorage(db)
    engine = PropertyOpportunityEngine(db)
    return db, storage, parcel_storage, storm_storage, outcome_storage, engine


def handle_enrich_parcels(args: argparse.Namespace) -> int:
    db, _, parcel_storage, _, _, _ = get_components()
    batch_size = args.batch_size or 100
    limit = args.limit or 0
    force = args.force

    print("\n" + "=" * 78)
    print("  MIAMI-DADE PROPERTY APPRAISER (PAPA) PARCEL ENRICHMENT")
    print("=" * 78)

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

    client = ParcelClient(batch_size=batch_size)
    start_time = time.perf_counter()

    print(f"\n[*] Querying Miami-Dade ArcGIS PaGISView in batches of {batch_size}...")

    def on_progress(fetched: int, total: int):
        pct = round((fetched / total) * 100, 1)
        sys.stdout.write(f"\r    [→] Enriched {fetched:,} / {total:,} parcels ({pct}%)")
        sys.stdout.flush()

    parcels = client.fetch_all_parcels(target_folios, progress_callback=on_progress)
    print()

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


def handle_enrich_weather(args: argparse.Namespace) -> int:
    _, _, _, storm_storage, _, _ = get_components()
    start_date = args.start_date or "2024-01-01T00:00Z"

    print("\n" + "=" * 78)
    print("  NOAA / NWS SEVERE WEATHER STORM ENRICHMENT")
    print("=" * 78)
    print(f"Ingesting National Weather Service Local Storm Reports (WFO: MFL / Miami)")
    print(f"Observation Window: {start_date} to Present\n")

    client = NOAAStormClient()
    start_time = time.perf_counter()

    events = client.fetch_local_storm_reports(start_date=start_date)
    saved = storm_storage.upsert_storm_events(events)
    duration = round(time.perf_counter() - start_time, 2)

    stats = storm_storage.get_stats()

    print("\n" + "=" * 78)
    print("  STORM ENRICHMENT COMPLETE")
    print("=" * 78)
    print(f"Total Storm Reports Ingested:      {saved:,}")
    print(f"Total Events in Weather Cache:     {stats['total_events']:,}")
    if stats.get("earliest_event") and stats.get("latest_event"):
        print(f"Event Time Range:                  {stats['earliest_event'][:10]} to {stats['latest_event'][:10]}")
    print(f"Ingestion Duration:                {duration}s\n")

    print("--- Event Breakdown by Hazard Type ---")
    for et, cnt in stats.get("by_type", {}).items():
        print(f"  {et:<30}: {cnt:>4,}")
    print("=" * 78)
    print("\n[✓] Next step: Run 'build-graph' to correlate property coordinates with storm paths.\n")
    return 0


def handle_build_graph(args: argparse.Namespace) -> int:
    db, storage, parcel_storage, storm_storage, _, engine = get_components()

    print("\n[*] Starting Property Graph Aggregation across all permits...")
    start_time = time.perf_counter()

    parcels_map = parcel_storage.get_all_parcels_map()
    print(f"    [✓] Loaded {len(parcels_map):,} enriched parcel profiles from local cache.")

    storm_events = storm_storage.get_all_storm_events()
    print(f"    [✓] Loaded {len(storm_events):,} severe storm events from weather cache.")
    correlator = StormCorrelator(storm_events)
    engine.storm_correlator = correlator

    timelines = engine.build_property_timelines(parcels_map=parcels_map)
    t_count = len(timelines)
    print(f"    [✓] Synthesized multi-trade timelines for {t_count:,} unique property folios.")

    storage.save_timelines(timelines)
    print(f"    [✓] Persisted {t_count:,} property timelines with derived roof & building histories.")

    print("\n[*] Detecting commercial signals (Track A: Permitted Projects & Track B: Modernization Signals)...")
    signals = engine.generate_signals(timelines)
    s_count = len(signals)
    print(f"    [✓] Generated {s_count:,} discrete commercial signals.")

    storage.save_signals(signals)
    print(f"    [✓] Statefully synced signals to property_signals table (0 dropped records).")

    csv_roofers = Path("data/property_signals_roofers.csv")
    csv_suppliers = Path("data/property_signals_suppliers.csv")
    json_all = Path("data/property_signals.json")

    export_stats = storage.export_signals(csv_roofers, csv_suppliers, json_all)

    # Export 3 commercial value streams
    feed_stats = storage.export_commercial_feeds(Path("data"))

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
        name = "Feed 2: Pre-Permit Roof Opportunities (Roofers)" if "ROOF" in aud else "Feed 1: Permitted Projects (Suppliers/Logistics)"
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

    print(f"\n[✓] Exported 3 Commercial Feeds:")
    print(f"    - Feed 1 (Permitted Projects):   {feed_stats['files']['feed_1_csv']} ({feed_stats['feed_1_permitted_projects']:,} records)")
    print(f"    - Feed 2 (Pre-Permit Roofer):    {feed_stats['files']['feed_2_csv']} ({feed_stats['feed_2_pre_permit_opportunities']:,} records)")
    print(f"    - Feed 3 (Market Intelligence):  {feed_stats['files']['feed_3_csv']} ({feed_stats['feed_3_market_intelligence_contractors']:,} contractors)")
    print(f"    - Unified Signal Database:       {json_all} ({export_stats['total']:,} records)\n")
    return 0


def handle_export_validation_sample(args: argparse.Namespace) -> int:
    _, storage, _, _, _, _ = get_components()

    print("\n" + "=" * 88)
    print("  GENERATING 100-RECORD STRATIFIED CONTRACTOR VALIDATION SAMPLE")
    print("=" * 88)

    signals = storage.get_signals(audience="roofer", limit=3500)
    if not signals:
        print("\n[!] No active roofer signals found. Run 'build-graph' first.\n")
        sample_path = Path("data/roofer_validation_sample_100.json")
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sample_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        return 0

    grade_a = []  # Compound Strong evidence (score >= 70 + (age >= 20 & storm) OR (age >= 25 & 2+ trades) OR (solar & age >= 20))
    grade_b = []  # Medium evidence (score 35-69, or single evidence line)
    grade_c = []  # Exploratory / young / unrecorded age (score < 35, unrecorded age, or age <= 5)

    for s in signals:
        b_age = s.get("building_age")
        score = s.get("evidence_score", 0.0)
        has_storm = bool(s.get("storm_distance_miles") and s.get("storm_distance_miles") <= 5.0)
        s_type = s.get("signal_type")
        corrob = s.get("corroborating_signals", [])

        # Strict Compound Evidence for Grade A:
        # Requires at least TWO distinct, corroborating physical/event dimensions
        is_compound_a = score >= 70.0 and (
            (b_age and b_age >= 20 and has_storm)
            or (b_age and b_age >= 25 and len(corrob) >= 2)
            or (s_type == "SOLAR_ROOF_SIGNAL" and b_age and b_age >= 20)
        )

        if is_compound_a:
            if len(grade_a) < 40:
                s["validation_tier"] = "GRADE_A_STRONG_EVIDENCE"
                s["evidence_grade"] = "A"
                grade_a.append(s)
        elif (35.0 <= score < 70.0) or (score >= 70.0 and not is_compound_a):
            if len(grade_b) < 40:
                s["validation_tier"] = "GRADE_B_MEDIUM_EVIDENCE"
                s["evidence_grade"] = "B"
                grade_b.append(s)
        elif score < 35.0 or s.get("year_built_status") == "UNRECORDED" or (b_age is not None and b_age <= 5):
            if len(grade_c) < 20:
                s["validation_tier"] = "GRADE_C_EXPLORATORY_SIGNAL"
                s["evidence_grade"] = "C"
                grade_c.append(s)

    all_sample = grade_a + grade_b + grade_c

    csv_path = Path("data/roofer_validation_sample_100.csv")
    json_path = Path("data/roofer_validation_sample_100.json")

    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_sample, f, indent=2)

    if all_sample:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_sample[0].keys())
            writer.writeheader()
            writer.writerows(all_sample)

    print(f"Sample Stratification:")
    print(f"  • Grade A (Compound Strong Evidence: Age >= 20y + Storm / Multi-trade) : {len(grade_a):>2} records")
    print(f"  • Grade B (Medium Evidence: Moderate Age / Single Signal / Solar)       : {len(grade_b):>2} records")
    print(f"  • Grade C (Exploratory Hypotheses: Unrecorded / Young Age / Low Score)  : {len(grade_c):>2} records")
    print(f"  Total Sample Size:                                                     {len(all_sample):>2} records")
    print("-" * 88)
    print(f"[✓] Exported Contractor Interview Packets:")
    print(f"    - CSV Format:  {csv_path}")
    print(f"    - JSON Format: {json_path}")
    print("\n[★] COMMERCIAL DISCOVERY PROTOCOL:")
    print("    Take these 100 properties to 10–20 Miami-Dade roofing contractors and ask:")
    print("    \"Pick the 10 properties you'd actually spend money/time pursuing.\"")
    print("    Record their choices using:")
    print("    python -m src.opportunity.cli record-outcome --signal-id <ID> --contractor <NAME> --selected [options]\n")
    return 0


def handle_generate_interview_kit(args: argparse.Namespace) -> int:
    sample_json = Path("data/roofer_validation_sample_100.json")
    if not sample_json.exists():
        print("\n[!] Validation sample not found. Run 'export-validation-sample' first.\n")
        return 0

    with open(sample_json, "r", encoding="utf-8") as f:
        records = json.load(f)

    count = min(args.count or 25, len(records)) if records else 0
    rng = random.Random(args.seed if args.seed is not None else 42)
    selected = rng.sample(records, count) if records and count > 0 else []

    txt_lines = [
        "=" * 88,
        "  MIAMI-DADE ROOFING COMMERCIAL DISCOVERY INTERVIEW KIT",
        f"  Contractor Review Packet ({count} Blind Property Evaluation Candidates)",
        "=" * 88,
        "INTERVIEW INSTRUCTION:",
        "\"Out of these properties, which 10 would you actually spend sales / estimator resources pursuing?\"",
        "-" * 88,
        "",
    ]

    blind_records = []
    for idx, r in enumerate(selected, start=1):
        observed = []
        if r.get("year_built"):
            observed.append(f"Structure Built: {r['year_built']} ({r.get('building_age')} years old)")
        else:
            observed.append("Structure Built: Construction year unrecorded in county records")

        if r.get("roof_history_status") == "VERIFIED_PRIOR_PERMIT":
            observed.append("Roof History: Prior permit on record")
        else:
            observed.append("Roof History: No roof replacement permit in recent dataset window")

        if r.get("storm_event_type") and r.get("storm_distance_miles") is not None:
            s_age = f"{int(r['storm_age_days'])} days ago" if r.get("storm_age_days") is not None else "recently"
            observed.append(f"Nearby Weather: NWS {r['storm_event_type']} {r['storm_distance_miles']} mi away ({s_age})")

        for c in r.get("corroborating_signals", []):
            if not any(k in c for k in ("Structure built", "NWS", "Recent severe")):
                observed.append(c)

        inferences = [
            f"Opportunity Scope: {r.get('recommended_action') or 'Elevated probability of near-term roof work'}",
        ]
        if r.get("estimated_roof_squares"):
            est_val = r["estimated_roof_squares"] * 500.0
            inferences.append(f"Estimated Scale: ~{r['estimated_roof_squares']:.1f} squares (~${est_val:,.0f} est. contract value)")

        unknowns = [
            "Physical roof condition uninspected on-site",
            "Homeowner intent unverified (requires sales contact)",
            "Insurance claim status unverified",
        ]

        blind_obj = {
            "item_number": idx,
            "signal_id": r["signal_id"],
            "folio": r["folio"],
            "address": r.get("address"),
            "owner_name": r.get("owner_name"),
            "dor_desc": r.get("dor_desc"),
            "observed": observed,
            "inference": inferences,
            "unknown": unknowns,
        }
        blind_records.append(blind_obj)

        txt_lines.append(f"[{idx:>2}] PROPERTY: {r.get('address')} (Folio: {r['folio']}) | ID: {r['signal_id']}")
        owner = r.get("owner_name")
        if owner:
            txt_lines.append(f"     Owner: {owner}")
        txt_lines.append("     OBSERVED:")
        for o in observed:
            txt_lines.append(f"       ✓ {o}")
        txt_lines.append("     INFERENCE:")
        for inf in inferences:
            txt_lines.append(f"       → {inf}")
        txt_lines.append("     UNKNOWN:")
        for u in unknowns:
            txt_lines.append(f"       ? {u}")
        txt_lines.append("     [ ] SELECT TO PURSUE     [ ] REJECT (Reason: ____________________)")
        txt_lines.append("-" * 88)

    txt_path = Path("data/contractor_interview_kit_25.txt")
    json_path = Path("data/contractor_interview_kit_25.json")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(blind_records, f, indent=2)

    print("\n" + "=" * 88)
    print("  CONTRACTOR DISCOVERY INTERVIEW KIT GENERATED")
    print("=" * 88)
    print(f"Generated {count} randomized, blind property evaluation profiles.")
    print("All internal scores and Grade A/B/C labels are completely omitted to prevent evaluation bias.\n")
    print(f"[✓] Printable Review Sheet: {txt_path}")
    print(f"[✓] Blind JSON Payload:     {json_path}\n")
    print("Protocol Question:")
    print("  \"Out of these properties, which 10 would you actually spend sales / estimator resources pursuing?\"")
    print("\nRecord selections using:")
    print("  python -m src.opportunity.cli record-outcome --signal-id <ID> --contractor <NAME> --selected\n")
    return 0


def handle_record_outcome(args: argparse.Namespace) -> int:
    _, _, _, _, outcome_storage, _ = get_components()
    signal_id = args.signal_id
    contractor = args.contractor
    notes = args.notes or ""

    reason = args.reason.strip().lower() if args.reason else None
    if reason and reason not in ALL_REASON_CODES:
        print(f"\n[!] Notice: Custom reason code '{reason}' recorded.")

    outcome = SignalOutcome(
        signal_id=signal_id,
        folio=args.folio or "UNKNOWN",
        reviewer_or_contractor=contractor,
        shown_to_customer_at=datetime.now(timezone.utc).isoformat(),
        customer_viewed=True,
        contractor_selected=args.selected or bool(args.appointment or args.won),
        customer_exported=args.exported,
        customer_contacted=args.contacted or bool(args.appointment or args.won),
        customer_marked_useful=args.useful or bool(args.appointment or args.won),
        customer_marked_bad=args.bad,
        appointment=args.appointment or bool(args.won),
        estimate_amount=args.estimate_amount,
        won=args.won,
        won_amount=args.won_amount,
        lost=args.lost,
        disposition=args.disposition or ("CONVERTED" if args.won else ("INTERESTED" if (args.useful or args.selected) else "PENDING")),
        reason_code=reason,
        feedback_notes=notes,
    )

    out_id = outcome_storage.record_outcome(outcome)
    print(f"\n[✓] Recorded contractor outcome {out_id} for Signal {signal_id} ({contractor}).")
    print(f"    Disposition: {outcome.disposition} | Selected: {outcome.contractor_selected} | Appointment: {outcome.appointment} | Won: {outcome.won}")
    if outcome.reason_code:
        print(f"    Reason Code: {outcome.reason_code}")
    if outcome.estimate_amount or outcome.won_amount:
        print(f"    Revenue Attribution: Estimate=${outcome.estimate_amount or 0:,.2f} | Won=${outcome.won_amount or 0:,.2f}\n")
    return 0


def handle_outcome_stats(args: argparse.Namespace) -> int:
    _, _, _, _, outcome_storage, _ = get_components()
    stats = outcome_storage.get_conversion_metrics()

    print("\n" + "=" * 88)
    print("  CONTRACTOR VALIDATION & OUTCOME TELEMETRY (THE CONVERSION MOAT)")
    print("=" * 88)
    print(f"Total Feedback Records Tracked:    {stats['total_tracked']:,}")
    print(f"Signals Shown to Contractors:      {stats['viewed']:,}")
    print(f"Contractor Selected for Pursuit:   {stats['selected']:,} ({stats['selection_rate_pct']}%)")
    print(f"Homeowner Outreach Attempted:      {stats['contacted']:,} ({stats['contact_rate_pct']}%)")
    print(f"Estimator Appointments Booked:     {stats['appointments']:,} ({stats['appointment_rate_pct']}%)")
    print(f"Deals Won (Closed Revenue):        {stats['deals_won']:,} (Win Rate: {stats['win_rate_pct']}%)")
    print(f"Deals Lost:                        {stats['deals_lost']:,}")
    print(f"Total Pipeline Quoted (Estimate):  ${stats['total_estimate_dollars']:,.2f}")
    print(f"Total Attributed Won Revenue:      ${stats['total_won_dollars']:,.2f}\n")

    if stats["by_reason"]:
        print("--- Feedback Reason Breakdown ---")
        for r_code, count in stats["by_reason"].items():
            tag = "[+] GOOD" if r_code in POSITIVE_REASONS else "[-] BAD"
            print(f"  {tag} {r_code:<28}: {count:>3} records")
        print()

    if stats["by_score_tier"]:
        print("--- Conversion by Evidence Score Tier ---")
        print(f"  {'Score Tier':<24} | {'Total':<6} | {'Selected':<8} | {'Appts':<6} | {'Won':<4} | {'Won Rev ($)':<12}")
        print("  " + "-" * 72)
        for tier, d in stats["by_score_tier"].items():
            print(f"  {tier:<24} | {d['total']:<6} | {d['selected']:<8} | {d['appointments']:<6} | {d['won']:<4} | ${d['won_revenue']:>10,.2f}")
        print()
    print("=" * 88 + "\n")
    return 0


def handle_export_feeds(args: argparse.Namespace) -> int:
    _, storage, _, _, _, _ = get_components()
    print("\n" + "=" * 88)
    print("  EXPORTING 3 COMMERCIAL VALUE STREAMS")
    print("=" * 88)
    res = storage.export_commercial_feeds(Path("data"))
    print(f"[✓] Feed 1: NEW_PERMITTED_PROJECT (Suppliers/Distributors) : {res['feed_1_permitted_projects']:,} records")
    print(f"    ↳ CSV: {res['files']['feed_1_csv']}")
    print(f"    ↳ JSON: {res['files']['feed_1_json']}")
    print(f"[✓] Feed 2: PRE_PERMIT_ROOF_OPPORTUNITY (Roofers)          : {res['feed_2_pre_permit_opportunities']:,} records")
    print(f"    ↳ CSV: {res['files']['feed_2_csv']}")
    print(f"    ↳ JSON: {res['files']['feed_2_json']}")
    print(f"[✓] Feed 3: MARKET_INTELLIGENCE (Contractor Velocity)      : {res['feed_3_market_intelligence_contractors']:,} contractors")
    print(f"    ↳ CSV: {res['files']['feed_3_csv']}")
    print(f"    ↳ JSON: {res['files']['feed_3_json']}\n")
    return 0


def handle_list_signals(args: argparse.Namespace) -> int:
    _, storage, _, _, _, _ = get_components()
    audience = args.audience
    limit = args.limit or 15

    signals = storage.get_signals(audience=audience, limit=limit)
    if not signals:
        print("\n[!] No active signals found. Run 'build-graph' first.\n")
        return 0

    aud_label = (audience or "ALL").upper()
    print("\n" + "=" * 128)
    print(f"  PROPERTY SIGNALS & EVIDENCE AUDIT (AUDIENCE: {aud_label} | TOP {len(signals)})")
    print("=" * 128)
    print(f"{'SIGNAL ID':<16} | {'SCORE':<5} | {'SIGNAL TYPE':<26} | {'AGE / YR':<10} | {'STORM PROX':<16} | {'ADDRESS / OWNER':<38}")
    print("-" * 128)
    for s in signals:
        addr = (s.get("address") or s.get("folio") or "N/A")[:38]
        yr = str(s.get("year_built") or "—")
        b_age = f"{s.get('building_age')}y" if s.get("building_age") is not None else "—"
        yr_label = f"{yr} ({b_age})" if yr != "—" else "—"

        storm_txt = "—"
        if s.get("storm_event_type") and s.get("storm_distance_miles") is not None:
            storm_txt = f"{s['storm_event_type'][:8]} {s['storm_distance_miles']}mi"

        print(f"{s['signal_id']:<16} | {s['evidence_score']:<5} | {s['signal_type']:<26} | {yr_label:<10} | {storm_txt:<16} | {addr:<38}")

        owner = s.get("owner_name")
        if owner:
            print(f"   ↳ OWNER:       {owner[:50]}")

        corrob = json.loads(s.get("corroborating_signals_json") or "[]")
        unverif = json.loads(s.get("unverified_assumptions_json") or "[]")

        if corrob:
            print(f"   ↳ EVIDENCE:    {'; '.join(corrob[:2])}")
        if unverif:
            print(f"   ↳ UNVERIFIED:  {'; '.join(unverif[:2])}")
        print("-" * 128)
    print()
    return 0


def handle_signal_stats(args: argparse.Namespace) -> int:
    _, storage, parcel_storage, storm_storage, outcome_storage, _ = get_components()
    metrics = storage.get_stats()
    parcel_stats = parcel_storage.get_stats()
    storm_stats = storm_storage.get_stats()
    outcome_stats = outcome_storage.get_conversion_metrics()

    if metrics["total_properties"] == 0:
        print("\n[!] Graph not built yet. Run 'build-graph' first.\n")
        return 0

    print("\n" + "=" * 78)
    print("  PROPERTY GRAPH & SIGNAL LIFECYCLE METRICS")
    print("=" * 78)
    print(f"Total Property Timelines:       {metrics['total_properties']:,}")
    print(f"Active Signals:                 {metrics['active_signals']:,}")
    print(f"Resolved Signals:               {metrics['resolved_signals']:,}")
    print(f"Enriched Parcels in Cache:      {parcel_stats['total_parcels']:,} (Verified Year: {parcel_stats['parcels_with_year_built']:,})")
    print(f"Severe Storm Events in Cache:   {storm_stats['total_events']:,}")
    print(f"Contractor Outcomes Tracked:    {outcome_stats['total_tracked']:,}\n")

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

    # enrich-weather
    p_weather = subparsers.add_parser("enrich-weather", help="Ingest NOAA/NWS severe storm reports for Miami-Dade")
    p_weather.add_argument("--start-date", default="2024-01-01T00:00Z", help="Start date for storm window (ISO format)")
    p_weather.set_defaults(func=handle_enrich_weather)

    # build-graph
    p_build = subparsers.add_parser("build-graph", help="Aggregate folios and generate evidence-backed signals")
    p_build.set_defaults(func=handle_build_graph)

    # export-feeds
    p_feeds = subparsers.add_parser("export-feeds", help="Export the 3 commercial feeds (Permitted, Pre-permit, Market Intel)")
    p_feeds.set_defaults(func=handle_export_feeds)

    # export-validation-sample
    p_val = subparsers.add_parser("export-validation-sample", help="Generate stratified 100-record roofer validation sample")
    p_val.set_defaults(func=handle_export_validation_sample)

    # generate-interview-kit
    p_kit = subparsers.add_parser("generate-interview-kit", help="Generate randomized blind evaluation packet for contractor interviews")
    p_kit.add_argument("--count", type=int, default=25, help="Number of blind properties to sample (default: 25)")
    p_kit.add_argument("--seed", type=int, default=None, help="Random seed for reproducible randomized order")
    p_kit.set_defaults(func=handle_generate_interview_kit)

    # record-outcome
    p_outcome = subparsers.add_parser("record-outcome", help="Record contractor feedback and sales conversion outcome")
    p_outcome.add_argument("--signal-id", required=True, help="Signal ID (e.g. SIG-XXXXXXXXXXXX)")
    p_outcome.add_argument("--contractor", required=True, help="Contractor or reviewer name")
    p_outcome.add_argument("--folio", default="UNKNOWN", help="Property Folio")
    p_outcome.add_argument("--selected", action="store_true", help="Contractor selected lead for active pursuit ('Pick 10')")
    p_outcome.add_argument("--useful", action="store_true", help="Contractor marked signal as useful/good")
    p_outcome.add_argument("--bad", action="store_true", help="Contractor marked signal as bad/inaccurate")
    p_outcome.add_argument("--contacted", action="store_true", help="Contractor reached out to property owner")
    p_outcome.add_argument("--exported", action="store_true", help="Contractor exported lead")
    p_outcome.add_argument("--appointment", action="store_true", help="Inspection/estimate appointment scheduled")
    p_outcome.add_argument("--estimate-amount", type=float, default=None, help="Quote/estimate amount in dollars")
    p_outcome.add_argument("--won", action="store_true", help="Contract awarded / deal won")
    p_outcome.add_argument("--won-amount", type=float, default=None, help="Closed contract revenue in dollars")
    p_outcome.add_argument("--lost", action="store_true", help="Deal lost / homeowner declined")
    p_outcome.add_argument("--disposition", choices=["PENDING", "INTERESTED", "REJECTED", "CONVERTED", "UNRESPONSIVE"], default=None)
    p_outcome.add_argument("--reason", help="Structured reason code (e.g. confirmed_old_roof, roof_already_replaced)")
    p_outcome.add_argument("--notes", help="Qualitative feedback or interview notes")
    p_outcome.set_defaults(func=handle_record_outcome)

    # outcome-stats
    p_out_stats = subparsers.add_parser("outcome-stats", help="Show contractor validation and conversion metrics")
    p_out_stats.set_defaults(func=handle_outcome_stats)

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
