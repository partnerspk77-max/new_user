# Miami-Dade County Building Permits Data Ingestion Pipeline (Phase 1)

Production-ready, resilient data ingestion pipeline for Miami-Dade County Building Permits using the official Miami-Dade ArcGIS REST Feature Layer.

## 1. Official Source & Endpoint
- **Feature Layer**: County Building Permits @ BuildingPermit
- **MapServer Endpoint**: `https://gisweb.miamidade.gov/arcgis/rest/services/MD_LandInformation/MapServer/1`
- **Query Endpoint**: `https://gisweb.miamidade.gov/arcgis/rest/services/MD_LandInformation/MapServer/1/query`
- **Protocol**: HTTP GET ArcGIS REST query `f=json` with server-side WGS84 spatial projection (`outSR=4326`).

---

## 2. Architecture & Pipeline Stages

```
Miami-Dade ArcGIS REST API (MapServer/1/query)
                  │
                  ▼ (outSR=4326, f=json, pagination, exponential backoff)
           ArcGIS Client (HTTP GET, rate limits, retries on 429/5xx)
                  │
                  ▼
         Raw Ingestion & Storage (`raw_permits` table: source, object_id, global_id, fetched_at, raw_payload)
                  │
                  ▼
         Normalization & Validation (UTC date parsing, string trimming, ID preservation, WGS84 lat/lon)
                  │
                  ▼
     Deduplication & Deterministic Upsert (`permits` table: ON CONFLICT DO UPDATE)
                  │
                  ▼
        Checkpoint State Store (`sync_state` table: last_processed_date, records counts, status)
```

### Key Principles
- **Separation of Raw and Normalized Data**: Every API feature is stored verbatim in `raw_permits` for replayability and auditability.
- **Deterministic Deduplication**: Uses immutable source identifiers (`source`, `source_object_id`). Re-running ingestion never duplicates records.
- **Update Preservation**: Modifying permit attributes (contractor, status, inspection dates) updates the existing row and updates `updated_at`.
- **Classification Preservation**: Unaltered preservation of `CAT1`–`CAT10`, `DESC1`–`DESC10`, `PROPUSE`, `APPTYPE`, and `FFRMLINE` for future classifier phases.
- **Direct WGS84 Geometry**: Queries `outSR=4326` from the server so latitude and longitude are exact without approximate local projections.
- **Structured File-Separated Logging**: Logs separated into `logs/app.log` (INFO+), `logs/warn.log` (WARN only), and `logs/error.log` (ERROR only).

---

## 3. Quickstart & Installation

### Prerequisites
- Python 3.12+
- Git

### Setup
```bash
# Clone the repository
git clone https://github.com/balmen/permit-pipeline.git
cd permit-pipeline

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt
```

---

## 4. CLI Usage

The pipeline exposes a clean, scriptable CLI:

### 1. Historical Backfill (90–180 days)
Extracts historical permit records using date-based pagination and ordering by `ISSUDATE ASC, OBJECTID ASC`.
```bash
# Backfill last 90 days (default)
python -m src.cli backfill --days 90

# Or specify custom days / date window
python -m src.cli backfill --days 180
python -m src.cli backfill --start-date 2024-01-01 --end-date 2024-06-30
```

### 2. Incremental Synchronization
Resumes from the latest known `issued_at` in the database minus a configurable safety overlap window (default: 120 minutes) to capture late-arriving and updated records.
```bash
python -m src.cli sync --overlap-minutes 120
```

### 3. Discovery & Schema Inspection Tooling
Answers the 9 key schema and distribution questions:
- A. Distinct `TYPE` values
- B. Distinct `CAT1`–`CAT10` values
- C. Top 25 most frequent `DESC1`–`DESC10` values
- D. Most recent `ISSUDATE` values
- E. Daily permit counts
- F. Permit status distribution (`BPSTATUS`: Active, Expired, Finalized)
- G. Folio fill percentage
- H. Contractor fill percentage
- I. Geometry fill percentage

```bash
# Print formatted human-readable report
python -m src.cli inspect

# Or output raw JSON
python -m src.cli inspect --json
```

### 4. JSON Export
Exports normalized database records to JSON:
```bash
python -m src.cli export-json --output data/permits.json
```

---

## 5. Automated GitHub Actions Workflow & Public Repository Setup

A production, fully flexible GitHub Actions workflow is provided in [`.github/workflows/permit_sync.yml`](.github/workflows/permit_sync.yml).

### Public Repository & GitHub Secrets
Since the repository is public, sensitive endpoints, tokens, and credentials should not be exposed. Configure these in your repository at **Settings** -> **Secrets and variables** -> **Actions**:

| Secret Name | Required | Description |
|---|---|---|
| `MIAMI_DADE_PERMITS_URL` | Optional | ArcGIS REST MapServer query endpoint (overrides default). |
| `ARCGIS_API_TOKEN` | Optional | API token or Bearer credential for GIS queries (redacted in logs). |
| `DATABASE_URL` | Optional | External DB connection string (e.g., PostgreSQL, Supabase, Neon). Defaults to SQLite cache if omitted. |

### Workflow Capabilities
- **Scheduled Synchronization**: Runs daily at `06:00 UTC`.
- **Manual Triggers (`workflow_dispatch`)**:
  - `command`: Choice of `sync`, `backfill`, `inspect`, or `export-json`.
  - `days`: Configurable historical days for backfill.
  - `start_date` / `end_date`: Custom date range for targeted extraction.
  - `overlap_minutes`: Safety overlap window for incremental sync.
  - `commit_data`: Optional toggle to commit `data/permits.json` back to the repository.
- **State Persistence via Cache**: Automatically caches and restores `data/permits.db` between runs so incremental sync remembers the latest date without re-downloading history.
- **Automated Artifacts**: Publishes `data/permits.db`, `data/permits.json`, and execution logs (`logs/*.log`) on every run.

### Pushing Code to the Remote Repository
```bash
git add .
git commit -m "feat(pipeline): complete Phase 1 Miami-Dade permit ingestion with GitHub Actions"
git push -u origin main
```

---

## 6. Configuration Reference

Environment variables can be set in `.env` (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `MIAMI_DADE_PERMITS_URL` | official MapServer query URL | ArcGIS REST query endpoint |
| `PERMIT_PAGE_SIZE` | `1000` | Max records per request |
| `PERMIT_REQUEST_TIMEOUT` | `30` | Request timeout in seconds |
| `PERMIT_MAX_RETRIES` | `5` | Exponential backoff retry attempts |
| `PERMIT_RETRY_BACKOFF_FACTOR` | `1.5` | Backoff multiplier |
| `PERMIT_REQUEST_DELAY_SECONDS` | `0.2` | Delay between consecutive pages |
| `PERMIT_BACKFILL_DAYS` | `90` | Default days for backfill |
| `PERMIT_INCREMENTAL_OVERLAP_MINUTES` | `120` | Overlap window for incremental sync |
| `DATABASE_URL` | `sqlite:///data/permits.db` | Database connection URL |
| `LOG_DIR` | `logs` | Directory for file-based logs |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## 7. Testing & Verification

Run the test suite with coverage:
```bash
pytest -v --cov=src tests/
```
All 36 unit and integration tests cover:
- ArcGIS REST API response parsing
- Epoch milliseconds date conversion to timezone-aware UTC ISO 8601
- WGS84 coordinate extraction and bounds validation
- Pagination across multiple pages (>1000 records)
- Deterministic deduplication & updates
- Transient API retry behavior (HTTP 429, 503, timeouts)
- Discovery inspection metrics calculation
