"""
Command-Line Interface for Miami-Dade Roofing Permit Intelligence.
Commands:
- inspect-roofing: Profiles category/description frequencies and discovered codes
- classify-roofing: Runs candidate detection + classification across all records
- validate-roofing: Generates 50/50/50 human-review validation sample
- roofing-stats: Outputs comprehensive data quality, contractor, and duplicate reports
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from src.config import config
from src.roofing.classifier import RoofingClassifier
from src.roofing.detector import RoofingCandidateDetector
from src.roofing.profiler import RoofingProfiler
from src.roofing.storage import RoofingStorage
from src.storage.database import Database


def get_roofing_components():
    db = Database(config.database_url)
    db.initialize_schema()
    storage = RoofingStorage(db)
    detector = RoofingCandidateDetector()
    classifier = RoofingClassifier(cache_store=storage)
    profiler = RoofingProfiler(db)
    return db, storage, detector, classifier, profiler


def handle_inspect_roofing(args: argparse.Namespace) -> int:
    _, _, _, _, profiler = get_roofing_components()
    profile = profiler.profile_fields()

    print("\n" + "=" * 78)
    print("  MIAMI-DADE ROOFING DISCOVERY & CATEGORY PROFILING REPORT")
    print("=" * 78)
    print(f"Total Records Analyzed: {profile['total_records']:,}\n")

    print("--- [1] Discovered Primary Roofing Categories in Miami-Dade Data ---")
    for r in profile["roofing_categories"]:
        print(f"  CAT {r['category']:<5} | {r['description']:<42} | {r['count']:>5,} ({r['percentage']:>5.2f}%)")
    print()

    print("--- [2] Top 10 Permit Categories Overall ---")
    for c in profile["top_categories"][:10]:
        print(f"  CAT {c['category']:<5} | {c['description']:<42} | {c['count']:>5,} ({c['percentage']:>5.2f}%)")
    print()

    print("--- [3] Frequency of Roofing-Related Keywords in Comments (FFRMLINE) ---")
    for t in profile["common_roofing_terms"]:
        print(f"  Keyword '{t['term']:<12}': {t['matching_records']:>5,} records ({t['percentage']:>5.2f}%)")
    print()

    print("--- [4] Discovered Permit Types ---")
    for tp in profile["type_distribution"]:
        print(f"  {tp['value']:<15}: {tp['count']:>6,} ({tp['percentage']:>5.2f}%)")
    print("=" * 78 + "\n")
    return 0


def handle_classify_roofing(args: argparse.Namespace) -> int:
    db, storage, detector, classifier, _ = get_roofing_components()
    conn = db.get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM permits")
    permits = [dict(r) for r in cursor.fetchall()]
    total_permits = len(permits)

    print(f"\n[*] Starting candidate detection & classification on {total_permits:,} permits...")
    start_time = time.perf_counter()

    candidate_count = 0
    confirmed_roofing = 0
    rejected_count = 0
    ambiguous_count = 0
    rule_classified = 0
    ai_classified = 0

    job_type_counts = {}

    for i, permit in enumerate(permits, 1):
        cand = detector.evaluate(permit)
        if cand.is_candidate:
            candidate_count += 1
            classification = classifier.classify(permit, cand)

            if classification.classification_source == "rule":
                rule_classified += 1
            else:
                ai_classified += 1

            if classification.is_roofing:
                confirmed_roofing += 1
                storage.upsert_roofing_permit(permit, classification)
                job_type_counts[classification.job_type] = job_type_counts.get(classification.job_type, 0) + 1
                if classification.job_type == "AMBIGUOUS":
                    ambiguous_count += 1
            else:
                rejected_count += 1
        else:
            rejected_count += 1

        if i % 2500 == 0 or i == total_permits:
            print(f"    Progress: {i:,}/{total_permits:,} records processed...")

    duration = round(time.perf_counter() - start_time, 2)

    print("\n" + "=" * 78)
    print("  CLASSIFICATION COMPLETED SUCCESSFULLY")
    print("=" * 78)
    print(f"Total Processed:            {total_permits:,}")
    print(f"Roofing Candidates:         {candidate_count:,} ({round(candidate_count/total_permits*100, 2)}%)")
    print(f"Confirmed Roofing Permits:  {confirmed_roofing:,} ({round(confirmed_roofing/total_permits*100, 2)}%)")
    print(f"Rejected / Non-Roofing:     {rejected_count:,} ({round(rejected_count/total_permits*100, 2)}%)")
    print(f"Ambiguous Scope:            {ambiguous_count:,}")
    print()
    print(f"Provenance: Rule-based:     {rule_classified:,} | AI Semantic: {ai_classified:,}")
    print(f"Duration:                   {duration}s\n")

    print("--- Breakdown by Roofing Job Type ---")
    for jt, cnt in sorted(job_type_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {jt:<25}: {cnt:>5,} permits ({round(cnt/confirmed_roofing*100, 1)}%)")
    print("=" * 78 + "\n")
    return 0


def handle_validate_roofing(args: argparse.Namespace) -> int:
    _, _, _, _, profiler = get_roofing_components()
    json_path = Path("data/roofing_validation_sample.json")
    csv_path = Path("data/roofing_validation_sample.csv")

    print("[*] Generating human review validation sample (50 roofing, 50 non-roofing, 50 ambiguous)...")
    profiler.export_validation_sample(json_path, csv_path, sample_size=50)

    print(f"[✓] Validation samples saved to:")
    print(f"    - JSON: {json_path}")
    print(f"    - CSV:  {csv_path}\n")
    return 0


def handle_roofing_stats(args: argparse.Namespace) -> int:
    _, storage, _, _, profiler = get_roofing_components()

    quality = profiler.analyze_quality()
    if "error" in quality:
        print(f"\n[!] {quality['error']}\n")
        return 1

    contractors = profiler.analyze_contractors(top_n=15)
    duplicates = profiler.analyze_duplicate_and_project_patterns()

    print("\n" + "=" * 78)
    print("  MIAMI-DADE ROOFING INTELLIGENCE: COMPREHENSIVE FINAL REPORT")
    print("=" * 78)
    print(f"Confirmed Roofing Permits: {quality['total_roofing_permits']:,}\n")

    print("--- Data Completeness & Quality ---")
    print(f"  * Folio Fill Rate:       {quality['folio_fill_rate']}%")
    print(f"  * Contractor Fill Rate:  {quality['contractor_fill_rate']}%")
    print(f"  * Geometry Fill Rate:    {quality['geometry_fill_rate']}%")
    print(f"  * Oldest Roofing Permit: {quality['oldest_permit_date'][:10] if quality['oldest_permit_date'] else 'N/A'}")
    print(f"  * Newest Roofing Permit: {quality['newest_permit_date'][:10] if quality['newest_permit_date'] else 'N/A'}\n")

    print("--- Status Distribution ---")
    for s in quality["status_distribution"]:
        status_name = {"A": "Active (Ongoing/In-Progress)", "F": "Finalized (Passed Final Inspection)"}.get(s["status"], "Other")
        print(f"  {s['status']:<4} ({status_name:<34}): {s['count']:>5,} ({s['percentage']:>5.2f}%)")
    print()

    print("--- Property Use (RESCOMM) ---")
    for r in quality["rescomm_distribution"]:
        print(f"  {r['type']:<20}: {r['count']:>5,} ({r['percentage']:>5.2f}%)")
    print()

    print("--- Top 15 Roofing Contractors in Miami-Dade ---")
    print(f"  {'Contractor Name':<42} | {'Total':>5} | {'Active':>6} | {'Finalized':>9}")
    print("  " + "-" * 70)
    for c in contractors:
        print(f"  {c['contractor_name'][:42]:<42} | {c['permit_count']:>5} | {c['active_count']:>6} | {c['finalized_count']:>9}")
    print()

    print("--- Duplicate & Multi-Permit Project Patterns ---")
    print(f"  * Folios with Multiple Roofing Permits:  {duplicates['folios_with_multiple_permits_count']:,}")
    print(f"  * Same-Day Clustered Multi-Permit Sites: {duplicates['same_day_clustered_projects_count']:,}")
    print("=" * 78 + "\n")

    # Export clean datasets
    csv_out = Path("data/roofing_permits.csv")
    json_out = Path("data/roofing_permits.json")
    exported = storage.export_csv_and_json(csv_out, json_out)
    print(f"[✓] Exported {exported:,} confirmed roofing permits to:")
    print(f"    - {csv_out}")
    print(f"    - {json_out}\n")
    return 0


def handle_audit_roofing(args: argparse.Namespace) -> int:
    from src.roofing.audit import CommercialAuditEngine
    db, _, _, _, _ = get_roofing_components()
    engine = CommercialAuditEngine(db)

    print("\n[*] Running 200-record stratified commercial viability and precision audit...")
    records = engine.run_200_record_audit()
    metrics = engine.compute_audit_metrics(records)

    csv_path = Path("data/roofing_commercial_audit_200.csv")
    json_path = Path("data/roofing_commercial_audit_200.json")
    engine.export_audit_files(records, csv_path, json_path)

    print("\n" + "=" * 78)
    print("  COMMERCIAL ROOFING AUDIT & ACTIONABILITY REPORT (200 RECORD SAMPLE)")
    print("=" * 78)
    print(f"Total Records Audited:                  {metrics['total_audited']}")
    print(f"True Roofing Scope (Technical):         {metrics['true_roofing_count']} ({metrics['true_roofing_percentage']}%)")
    print(f"Classification Precision:               {metrics['classification_precision']}%")
    print(f"Licensed Contractor Already Attached:   {metrics['contractor_attached_count']} ({metrics['contractor_attached_percentage']}%)")
    print(f"Owner-Builder / Unassigned Contractor:  {metrics['owner_builder_count']} ({metrics['owner_builder_percentage']}%)")
    print(f"Fresh Records (<3 days from latest):    {metrics['fresh_less_than_3_days_count']} ({metrics['fresh_less_than_3_days_percentage']}%)\n")

    print(f"ACTUAL ACTIONABLE SALES OPPORTUNITY:    {metrics['actual_sales_opportunity_count']} ({metrics['actual_sales_opportunity_percentage']}%)")
    print("=" * 78)

    print("\n--- Commercial Opportunity Breakdown ---")
    for opp, count in sorted(metrics["opportunity_breakdown"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["total_audited"]) * 100, 1)
        print(f"  {opp:<35}: {count:>3} ({pct:>5.1f}%)")

    print("\n--- Freshness Distribution (Across Sample) ---")
    for tier, count in sorted(metrics["freshness_distribution"].items(), key=lambda x: x[1], reverse=True):
        pct = round((count / metrics["total_audited"]) * 100, 1)
        print(f"  {tier:<25}: {count:>3} ({pct:>5.1f}%)")

    print(f"\n[✓] Audit details saved to:")
    print(f"    - CSV:  {csv_path}")
    print(f"    - JSON: {json_path}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Miami-Dade Roofing Permit Intelligence & Classification CLI"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_inspect = subparsers.add_parser("inspect-roofing", help="Profile roofing categories and terms")
    p_inspect.set_defaults(func=handle_inspect_roofing)

    p_classify = subparsers.add_parser("classify-roofing", help="Classify permits into roofing job types")
    p_classify.set_defaults(func=handle_classify_roofing)

    p_validate = subparsers.add_parser("validate-roofing", help="Generate 50/50/50 human review sample")
    p_validate.set_defaults(func=handle_validate_roofing)

    p_stats = subparsers.add_parser("roofing-stats", help="Output comprehensive roofing analysis report")
    p_stats.set_defaults(func=handle_roofing_stats)

    p_audit = subparsers.add_parser("audit-roofing", help="Run 200-record commercial viability audit")
    p_audit.set_defaults(func=handle_audit_roofing)

    parsed_args = parser.parse_args()
    return parsed_args.func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
