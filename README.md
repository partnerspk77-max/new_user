# Miami-Dade Lead Generation Pipeline

**End-to-end sales-lead automation built on official Miami-Dade County building permit data.**

The pipeline ingests every building permit the county publishes, detects roofing activity, enriches each property with appraiser records and NOAA storm history, synthesizes evidence-scored opportunity signals, and exports ready-to-work lead feeds for two commercial audiences:

| Audience | What they get | Feed file |
|---|---|---|
| **Roofing contractors** | Properties showing modernization activity (electrical / HVAC / plumbing upgrades) that historically precede roof replacement — *before* a roofing permit is ever filed | `data/feed_pre_permit_roof_opportunities.csv` |
| **Suppliers & distributors** | Active permitted roofing projects happening **right now** — material delivery and logistics opportunities | `data/feed_permitted_projects_suppliers.csv` |
| **Sales leadership** | Contractor market intelligence: permit velocity, 30-day growth, market share per contractor | `data/feed_contractor_market_intelligence.csv` |

Every signal carries its **evidence breakdown**, so a sales team knows *why* a property was flagged — no black boxes.

---

## Quickstart

```bash
# 1. Clone and install
git clone https://github.com/partnerspk77-max/new_user.git
cd new_user
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .            # or: pip install -r requirements.txt

# 2. ONE command runs everything (ingest -> classify -> enrich -> signals -> feeds)
python -m src.cli run-all

# 3. Collect your deliverables
ls data/feed_*.csv
```

The very first run automatically performs a historical backfill (default: 90 days). Every later run performs an incremental sync from the last known permit date — no duplicates, no missed records, fully resumable.

> Prefer installed entry points? `pip install -e .` gives you `permit-pipeline`, `lead-signals` and `roofing-intel` commands (e.g. `permit-pipeline run-all`).

---

## The One-Shot Command

```bash
python -m src.cli run-all [options]
```

`run-all` orchestrates all six stages and prints a live progress report:

| Stage | What happens | Failure behavior |
|---|---|---|
| 1. Ingestion | Backfill (empty DB) or incremental sync | Retries with backoff; on hard failure continues with existing data |
| 2. Classification | Roofing detection across all permits | Warnings; continues |
| 3. Enrichment | Parcel attributes (Property Appraiser) + NOAA/NWS storm reports | **Degradable** — network errors are reported, cached data still used |
| 4. Graph & Signals | Property timelines + evidence-scored signals | Aborts remaining stages (nothing to export) |
| 5. Feed Export | CSV/JSON lead feeds for both audiences | Reported; pipeline continues |
| 6. Audit & Metrics | 200-record viability audit + lifecycle stats | Warnings; continues |

Useful flags:

```bash
python -m src.cli run-all --skip-enrichment      # fast local-only run (no network)
python -m src.cli run-all --force-backfill       # re-pull full history
python -m src.cli run-all --days 180             # deeper first-run history
python -m src.cli run-all --enrich-limit 500     # cap parcel API calls per run
```

---

## Daily Operations (Optional Fine-Grained Commands)

All commands are idempotent and safe to re-run.

### Ingestion — `python -m src.cli`

| Command | Purpose |
|---|---|
| `backfill --days 90` | Pull historical permits (supports `--start-date` / `--end-date`) |
| `sync --overlap-minutes 360` | Incremental sync from latest known permit with safety overlap |
| `inspect` | Discovery report on the local dataset (`--json` for machine output) |
| `export-json --output data/permits.json` | Export normalized permits |

### Roofing Intelligence — `python -m src.roofing.cli`

| Command | Purpose |
|---|---|
| `classify-roofing` | Detect + classify roofing permits (rule + semantic engine) |
| `inspect-roofing` | Profile roofing categories & keyword frequencies |
| `validate-roofing` | Generate 50/50/50 human-review sample |
| `roofing-stats` | Data quality, contractor and duplicate reports + clean exports |
| `audit-roofing` | 200-record stratified commercial viability audit |

### Signal Engine — `python -m src.opportunity.cli`

| Command | Purpose |
|---|---|
| `enrich-parcels` | Fetch building age / area / owner from Property Appraiser (cached, resumable) |
| `enrich-weather` | Ingest NOAA/NWS severe storm reports for Miami-Dade |
| `build-graph` | Aggregate timelines, correlate storms, persist stateful signals + feeds |
| `export-feeds` | Re-export the 3 commercial feeds |
| `list-signals --audience roofer` | Browse signals with evidence breakdowns (`--limit 0` = all) |
| `signal-stats` | Lifecycle metrics (active, resolved, freshness) |
| `export-validation-sample` | Stratified 100-record (Grade A/B/C) review sample |
| `generate-interview-kit --count 25` | Blind contractor evaluation packet |
| `record-outcome --signal-id SIG-XXXX --contractor NAME --selected` | Log feedback & conversion outcomes |
| `outcome-stats` | Selection / appointment / win-rate telemetry |

---

## Fully Automated on GitHub Actions

The repository ships with a production workflow (`.github/workflows/permit_sync.yml`):

- **Daily at 06:00 UTC** — full incremental sync + discovery report + dataset export
- **Manual dispatch** — choose `sync`, `run-all` (full lead generation), `backfill`, `inspect`, or `export-json`
- **On every push** — hermetic test suite + incremental sync smoke test
- **Artifacts** — every run uploads the SQLite database, normalized JSON, the 3 lead feeds, and execution logs (30-day retention)
- **Caching** — the SQLite database is checkpointed and cached between runs, so scheduled syncs are incremental

Setup (optional — works out of the box with defaults):

1. Repo → *Settings → Secrets and variables → Actions*
2. Optionally add secrets: `ARCGIS_API_TOKEN`, `DATABASE_URL` (any SQLite path; PostgreSQL-ready by design)
3. Tune behavior with repository *variables*: `PERMIT_PAGE_SIZE`, `PERMIT_BACKFILL_DAYS`, `PERMIT_MAX_RETRIES`, `LOG_LEVEL`, …

---

## Configuration (`.env`)

Copy `.env.example` → `.env` and adjust. Everything is optional; sensible defaults are built in.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/permits.db` | Storage location |
| `PERMIT_BACKFILL_DAYS` | `90` | Historical window for first run |
| `PERMIT_INCREMENTAL_OVERLAP_MINUTES` | `360` | Re-fetch window to absorb timezone/data lag |
| `PERMIT_PAGE_SIZE` | `1000` | ArcGIS pagination size |
| `PERMIT_REQUEST_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `PERMIT_MAX_RETRIES` | `5` | Retry attempts with exponential backoff |
| `ARCGIS_API_TOKEN` | *(empty)* | Only if the GIS endpoint requires auth |
| `LOG_DIR` / `LOG_LEVEL` | `logs` / `INFO` | Structured logging (JSON included) |

---

## Data & Evidence Model

- **Raw layer** — every API response is archived verbatim in `raw_permits` (immutable audit trail).
- **Normalized layer** — cleaned permits with deterministic dedup keys and update tracking.
- **Intelligence layer** — property timelines, derived roof history, storm proximity, evidence scores.
- **Epistemic honesty** — each signal separates **OBSERVED evidence**, **UNVERIFIED assumptions**, and **UNKNOWN** gaps. Data never overstates what it knows.

Full table and column reference: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)

---

## Architecture

```
Miami-Dade ArcGIS REST ─┐
NOAA/NWS Storm Reports ─┼─> Ingestion ─> Normalize ─> SQLite (WAL)
Property Appraiser GIS ─┘                     │
                                              ▼
                             Roofing Classifier (rule + semantic)
                                              │
                                              ▼
                          Property Graph (per-folio permit timelines)
                                              │
                                              ▼
                    Evidence Engine (scores, grades, stateful lifecycle)
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                        Roofer Feed     Supplier Feed    Market Intel
```

Key properties:

- **Idempotent** — every command can crash or be re-run safely; checkpoints resume where they left off.
- **Respectful scraping** — throttled, retryable, identifiable `User-Agent`, only the official public endpoints.
- **Observable** — structured event logs (`logs/`) for every job, page, and record.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -v            # 80+ tests, fully hermetic (in-memory DB, mocked network)
```

The suite covers normalization, pagination, classification, storage dedup, signal scoring, outcome telemetry, and all CLI surfaces — including empty-database edge cases.

---

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `Ingestion failed ... timed out` | County GIS unreachable | Re-run later; nothing corrupted. CI runners usually reach it fine. |
| `PARCEL ENRICHMENT INCOMPLETE: X/Y` | Some batches failed mid-run | Re-run `enrich-parcels`; cached folios are skipped automatically. |
| `STORM ENRICHMENT FAILED` | NOAA/NWS endpoint issue | Re-run `enrich-weather` later; local pipeline unaffected. |
| `Unsupported DATABASE_URL` | A `.env` in a parent folder sets a non-SQLite URL | Fix or remove it; supported forms are documented above. |
| Feeds exist but have 0 rows | Graph not built yet | Run `python -m src.cli run-all`. |

---

## License

MIT — see [LICENSE](LICENSE).
