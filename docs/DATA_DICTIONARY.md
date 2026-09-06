# Data Dictionary

Reference for every table in the SQLite database (default: `data/permits.db`) and every export feed. All timestamps are ISO-8601 strings, UTC unless noted.

---

## Layer 1 — Raw & Normalized Permits

### `raw_permits` — immutable audit log

Every API response is archived here verbatim before any transformation. If a bug is ever found in normalization, this table allows a lossless rebuild.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Autoincrement |
| `source` | TEXT | Endpoint identifier (e.g. `miami_dade_arcgis`) |
| `source_object_id` | INTEGER | County OBJECTID of the feature |
| `global_id` | TEXT | County GlobalID |
| `fetched_at` | TEXT | Ingestion timestamp |
| `raw_payload` | TEXT | Original JSON feature, untouched |

### `permits` — normalized permits (deduplicated)

One row per county permit record. Deduplicated on `(source, source_object_id)`.

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PK | Deterministic ID (source + object id) |
| `source` / `source_object_id` / `global_id` | | Provenance & dedup key |
| `folio` | TEXT | Miami-Dade property folio (primary join key to parcels) |
| `permit_number` / `process_number` | TEXT | County permit identifiers |
| `address` / `unit` / `is_condo` | TEXT | Site address |
| `permit_type` | TEXT | Permit type code |
| `category_1` … `category_10`, `description_1` … `description_10` | TEXT | Up to 10 trade category/description pairs on one permit |
| `issued_at` | TEXT | Permit issue date (drives incremental sync) |
| `last_inspection_at` / `renewal_at` / `completion_at` / `last_approval_at` | TEXT | Lifecycle dates |
| `residential_commercial` | TEXT | RESCOMM flag |
| `proposed_use` / `application_type` | TEXT | Property use, application type |
| `comment` | TEXT | Free-form scope line (FFRMLINE) |
| `master_permit_number` | TEXT | Master permit for sub-permits |
| `contractor_number` / `contractor_name` | TEXT | Attached contractor (NULL ⇒ owner-builder) |
| `status` | TEXT | `A` = Active, `F` = Finalized |
| `latitude` / `longitude` | REAL | Site coordinates (WGS84) |
| `source_fetched_at` / `created_at` / `updated_at` | TEXT | Provenance timestamps |

### `sync_state` — ingestion checkpoint

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PK | Job id (`miami_dade_arcgis`) |
| `last_processed_date` | TEXT | Newest permit date seen — next sync resumes from here minus overlap |
| `last_source_object_id` / `total_ingested` | | Pagination guard + lifetime counter |
| `last_run_at` / `status` / `metadata_json` | | Last run result (`completed` / `failed` + metrics) |

---

## Layer 2 — Roofing Intelligence

### `roofing_permits` — confirmed roofing permits

Populated by `classify-roofing`. One row per permit confirmed as roofing work.

| Column | Type | Description |
|---|---|---|
| `permit_id` | TEXT PK | Joins to `permits.id` |
| `roofing_job_type` | TEXT | e.g. `RE_ROOF`, `NEW_ROOF`, `REPAIR`, `AMBIGUOUS` |
| `roofing_confidence` | REAL | Classifier confidence 0–1 |
| `classification_source` | TEXT | `rule` (category code) or `semantic` (keyword engine) |
| `classification_reason` / `classification_model` / `classification_version` | TEXT | Full provenance of the decision |
| remaining columns | | Subset of `permits` columns for fast querying |

### `roofing_ai_cache` — classification cache

Caches classifier decisions keyed by permit content hash so re-runs skip already-classified text.

---

## Layer 3 — Enrichment

### `property_parcels` — Property Appraiser cache

Populated by `enrich-parcels` (batched, resumable; re-running skips cached folios).

| Column | Type | Description |
|---|---|---|
| `folio` | TEXT PK | Property folio |
| `year_built` | INTEGER | Structure construction year → building age |
| `building_actual_area` / `building_heated_area` | REAL | Square footage (roof-scale estimation) |
| `bedroom_count` / `bathroom_count` | | Residential indicators |
| `dor_code` / `dor_desc` | TEXT | Department of Revenue land-use code & description |
| `owner_name` | TEXT | Owner of record (mailing decision-maker) |
| `site_address` / `site_zip` / `mailing_address` | TEXT | Site & owner mailing addresses |
| `assessed_value` | REAL | Current assessed value |
| `is_vacant` | INTEGER | Vacancy indicator |

### `storm_events` — NOAA/NWS severe weather cache

Populated by `enrich-weather` (NWS Local Storm Reports, WFO Miami).

| Column | Type | Description |
|---|---|---|
| `event_id` | TEXT PK | NWS report identifier |
| `event_type` | TEXT | HAIL, TSTM WND DMG, TORNADO, HIGH WIND, … |
| `magnitude` / `unit` | REAL/TEXT | e.g. 1.75 IN hail, 61 KT gust |
| `event_time` / `latitude` / `longitude` / `city` / `county` / `remark` | | Report details |

---

## Layer 4 — Signals & Outcomes

### `property_timelines` — per-folio permit history graph

One row per property folio; the backbone for evidence scoring.

| Column | Type | Description |
|---|---|---|
| `folio` | TEXT PK | Property |
| `total_permits` / `trades_json` / `active_trades_json` | | Complete & active trade activity |
| `roof_permits_count` / `has_active_roof_permit` / `last_roof_permit_date` / `years_since_last_roof_permit` / `last_roof_system` / `last_roof_contractor` | | Derived roof history |
| `has_active_non_roof_permit` / `non_roof_renovations_json` | | Modernization activity (pre-permit signal driver) |
| `contractors_json` | TEXT | All contractors seen on the property |
| `year_built` / `building_actual_area` / `building_heated_area` / `dor_desc` / `owner_name` / `assessed_value` | | Joined parcel attributes |

### `property_signals` — evidence-scored opportunities

The core commercial table. Also exported as `property_signals.json` / `property_signals_roofers.csv` / `property_signals_suppliers.csv`.

| Column | Type | Description |
|---|---|---|
| `signal_id` | TEXT PK | Stable ID (`SIG-XXXXXXXXXXXX`) |
| `folio` / `address` | | Property |
| `signal_type` | TEXT | `ACTIVE_ROOF_PROJECT`, `NEW_ROOF_PERMIT`, `PROPERTY_RENOVATION_SIGNAL`, `SOLAR_ROOF_SIGNAL`, `OWNER_BUILDER_ROOF_SIGNAL` |
| `target_audience` | TEXT | `ROOFING_CONTRACTOR` or `SUPPLIER_DISTRIBUTOR` |
| `status` | TEXT | `ACTIVE`, `RESOLVED`, `EXPIRED` (stateful lifecycle) |
| `first_detected_at` / `last_seen_at` | TEXT | Preserved across re-runs (statefulness) |
| `evidence_score` | REAL | 0–100 weighted evidence score |
| `evidence_breakdown_json` | TEXT | Per-dimension weights: renovation velocity, roof history, freshness, property scale, storm evidence |
| `corroborating_signals_json` | TEXT | Cross-trade corroboration list |
| `unverified_assumptions_json` | TEXT | Explicitly flagged assumptions (epistemic honesty) |
| `freshness_tier` / `age_hours` / `age_days` | | NEW (0-24h), FRESH (1-3d), RECENT (4-7d), STALE (8-30d), HISTORICAL (30d+) |
| `contractor_present` / `contractor_name` | | Competitor presence on the permit |
| `trigger_trade` / `trigger_event` / `recommended_action` | TEXT | What fired the signal and the suggested next action |
| `year_built` / `building_age` / `year_built_status` / `roof_history_status` | | Corroborated building age & roof history status |
| `building_actual_area` / `estimated_roof_squares` | REAL | Roof size estimate (squares) |
| `owner_name` / `dor_desc` / `assessed_value` | | Owner & property profile |
| `storm_event_type` / `storm_distance_miles` / `storm_event_date` / `storm_age_days` / `storm_magnitude` | | Nearest qualifying storm event to the property |

### `signal_outcomes` — conversion telemetry (the data moat)

Populated by `record-outcome`; consumed by `outcome-stats`.

| Column | Type | Description |
|---|---|---|
| `outcome_id` / `signal_id` / `folio` | | Which signal the feedback refers to |
| `reviewer_or_contractor` | TEXT | Who evaluated it |
| `customer_viewed` / `contractor_selected` / `customer_exported` / `customer_contacted` / `customer_marked_useful` / `customer_marked_bad` | INTEGER | Feedback funnel flags |
| `appointment` | INTEGER | Estimator appointment booked |
| `estimate_amount` / `won_amount` | REAL | Quoted and won revenue attribution |
| `won` / `lost` / `disposition` | | `CONVERTED`, `INTERESTED`, `PENDING`, … |
| `reason_code` / `feedback_notes` | | Standardized win/loss reasons + free text |

---

## Export Feeds (`data/`)

| File | Audience | Contents |
|---|---|---|
| `feed_pre_permit_roof_opportunities.csv` / `.json` | Roofing contractors | Active properties with modernization activity and no current roofing permit — get there first |
| `feed_permitted_projects_suppliers.csv` / `.json` | Suppliers & distributors | Active roofing projects with contractor name — material supply & logistics targets |
| `feed_contractor_market_intelligence.csv` / `.json` | Sales leadership | Contractor-level velocity, 30-day growth, market share |
| `property_signals_roofers.csv` / `property_signals_suppliers.csv` / `property_signals.json` | All | Full signal database split by audience |
| `roofing_permits.csv` / `.json` | Analysis | Confirmed roofing permits with classification provenance |
| `roofing_commercial_audit_200.csv` / `.json` | QA | Stratified 200-record viability audit |
| `roofer_validation_sample_100.csv` / `.json` | QA | Grade A/B/C validation sample |
| `permits.json` | Integrations | Full normalized permit export |

Feed columns mirror the `property_signals` table columns listed above (JSON fields are embedded as strings; parse with standard CSV + JSON tooling).

---

## Known Limitations

- Permit data reflects only what the county publishes; owner-builder permits carry no contractor.
- `estimated_roof_squares` is a heuristic from building footprint, not a measurement.
- Storm correlation is limited to the NWS storm-report archive window ingested so far.
- The pipeline is SQLite-first; PostgreSQL URLs are rejected by design until the driver is extended.
