"""
Database connection and schema management.
Supports SQLite (local file & in-memory for testing) and is extensible for PostgreSQL.
Enables WAL mode for crash recovery and concurrency.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src.client.exceptions import StorageError
from src.config import config
from src.logger import logger


SCHEMA_SQL = """
-- 1. Raw Permitted Data (Immutable Audit Log)
CREATE TABLE IF NOT EXISTS raw_permits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_object_id INTEGER NOT NULL,
    global_id TEXT,
    fetched_at TEXT NOT NULL,
    raw_payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_permits_source_obj ON raw_permits (source, source_object_id);
CREATE INDEX IF NOT EXISTS idx_raw_permits_fetched_at ON raw_permits (fetched_at);

-- 2. Normalized Permits Table
CREATE TABLE IF NOT EXISTS permits (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_object_id INTEGER NOT NULL,
    global_id TEXT,
    folio TEXT,
    permit_number TEXT,
    process_number TEXT,
    address TEXT,
    unit TEXT,
    is_condo TEXT,
    permit_type TEXT,
    category_1 TEXT,
    description_1 TEXT,
    category_2 TEXT,
    description_2 TEXT,
    category_3 TEXT,
    description_3 TEXT,
    category_4 TEXT,
    description_4 TEXT,
    category_5 TEXT,
    description_5 TEXT,
    category_6 TEXT,
    description_6 TEXT,
    category_7 TEXT,
    description_7 TEXT,
    category_8 TEXT,
    description_8 TEXT,
    category_9 TEXT,
    description_9 TEXT,
    category_10 TEXT,
    description_10 TEXT,
    issued_at TEXT,
    last_inspection_at TEXT,
    renewal_at TEXT,
    completion_at TEXT,
    last_approval_at TEXT,
    residential_commercial TEXT,
    proposed_use TEXT,
    application_type TEXT,
    comment TEXT,
    master_permit_number TEXT,
    contractor_number TEXT,
    contractor_name TEXT,
    status TEXT,
    latitude REAL,
    longitude REAL,
    source_fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT uq_source_object_id UNIQUE (source, source_object_id)
);

CREATE INDEX IF NOT EXISTS idx_permits_issued_at ON permits (issued_at);
CREATE INDEX IF NOT EXISTS idx_permits_permit_num ON permits (permit_number);
CREATE INDEX IF NOT EXISTS idx_permits_process_num ON permits (process_number);
CREATE INDEX IF NOT EXISTS idx_permits_folio ON permits (folio);
CREATE INDEX IF NOT EXISTS idx_permits_status ON permits (status);
CREATE INDEX IF NOT EXISTS idx_permits_type ON permits (permit_type);

-- 3. Ingestion State & Checkpoint
CREATE TABLE IF NOT EXISTS sync_state (
    id TEXT PRIMARY KEY,
    last_processed_date TEXT,
    last_source_object_id INTEGER,
    last_run_at TEXT,
    total_ingested INTEGER DEFAULT 0,
    status TEXT,
    metadata_json TEXT
);
"""


class Database:
    """Encapsulates SQLite connection lifecycle and schema initialization."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or config.database_url
        self._path = self._parse_sqlite_path(self.db_url)
        self._connection: Optional[sqlite3.Connection] = None

    @staticmethod
    def _parse_sqlite_path(url: str) -> str:
        """
        Parses supported DATABASE_URL values into a filesystem path.

        Supported formats:
          - sqlite:///relative/path.db   -> relative/path.db
          - sqlite:////absolute/path.db  -> /absolute/path.db
          - sqlite://:memory: / :memory: -> :memory:
          - bare filesystem path         -> used as-is (with a warning)
        Anything else (postgres://, mysql://, file://) raises StorageError with
        an actionable message instead of silently writing to a bogus file.
        """
        if url.startswith("sqlite:///"):
            path_str = url.replace("sqlite:///", "", 1)
            # sqlite:////abs/path keeps a leading slash after the first strip,
            # so a 4-slash form yields an absolute path.
            return path_str if url.startswith("sqlite:////") else path_str
        elif url == ":memory:" or url.startswith("sqlite://:memory:"):
            return ":memory:"

        lowered = url.lower()
        unsupported_prefixes = ("postgres://", "postgresql://", "mysql://", "file:", "mssql://", "oracle://")
        if lowered.startswith(unsupported_prefixes):
            raise StorageError(
                f"Unsupported DATABASE_URL: '{url}'. "
                f"This pipeline's SQLite driver supports: sqlite:///data/permits.db, "
                f"sqlite:///:memory:, or a plain filesystem path. "
                f"Fix the DATABASE_URL environment variable (check .env files in this and parent directories)."
            )
        if "://" in url:
            raise StorageError(
                f"Unrecognized DATABASE_URL scheme in '{url}'. "
                f"Expected 'sqlite:///path/to.db', ':memory:', or a plain filesystem path."
            )
        logger.warning(f"DATABASE_URL '{url}' is a bare filesystem path; treating it as SQLite database location.")
        return url

    def get_connection(self) -> sqlite3.Connection:
        """Returns active thread-safe sqlite3 Connection with row factory."""
        if self._connection is None:
            if self._path != ":memory:":
                db_path = Path(self._path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
            
            conn = sqlite3.connect(
                self._path,
                timeout=30.0,
                check_same_thread=False,
                isolation_level=None,  # Autocommit mode / manual BEGIN
            )
            conn.row_factory = sqlite3.Row

            # Performance & reliability pragmas
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")

            self._connection = conn

        return self._connection

    def initialize_schema(self) -> None:
        """Executes DDL to ensure all tables and indexes exist."""
        conn = self.get_connection()
        with conn:
            conn.executescript(SCHEMA_SQL)
        logger.info(f"Database schema initialized at {self._path}")

    def close(self) -> None:
        """Closes open connection if any."""
        if self._connection:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None
