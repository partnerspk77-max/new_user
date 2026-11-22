#!/usr/bin/env python3
"""archive-relay — fetch a ZIP of CSV tables, parse it, deliver JSON.

Flow: GET SOURCE_URL -> unzip -> parse every table at runtime -> POST the rows
to TARGET_URL as JSON (a begin manifest, per-table chunks, a completion notice).

Properties:
- Python 3.12+, standard library only (nothing to install).
- Manual runs only; this script does no scheduling.
- Nothing is kept: the archive and parsed rows live in WORK_DIR, which is
  wiped before and after every run. Only the audit log survives, for
  diagnostics; it never contains secret values or (by default) table names.
- Every outbound request is retried with exponential backoff and jitter.
- Any 2xx response from the endpoint counts as delivered.

Configuration (environment):
  SOURCE_URL           (required) URL of the ZIP archive
  SOURCE_TOKEN         (optional) bearer token for the source
  TARGET_URL           (required unless DRY_RUN) delivery endpoint
  TARGET_TOKEN         (required unless DRY_RUN) bearer token for the endpoint
  TARGET_GZIP          "true" to gzip request bodies (Content-Encoding: gzip)
  DEBUG_NAMES          "true" to log plain table names instead of hashes
  DRY_RUN              "true" to download+parse only, no delivery requests
  ROWS_PER_POST        rows per delivery request (default 10000)
  HTTP_TIMEOUT         per-request timeout in seconds (default 300)
  MAX_UNCOMPRESSED_MB  cap on decompressed archive size (default 2048)
  WORK_DIR             scratch dir; default is a fresh temp dir. Wiped
                       before and after the run.
  AUDIT_DIR            audit log directory (default ./audit-log); not wiped.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import http.client
import json
import os
import random
import shutil
import socket
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "archive-relay/v1"
USER_AGENT = "archive-relay/1.0"
TABLE_EXTENSIONS = {".txt", ".csv"}
GET_ATTEMPTS = 4
POST_ATTEMPTS = 5
RETRY_BASE_SECONDS = 2.0
RETRY_CAP_SECONDS = 30.0
RETRYABLE_STATUS = {408, 429}


class PipelineError(Exception):
    """Any expected failure (config, source, parse, delivery)."""


class ConfigError(PipelineError):
    """Bad or missing configuration."""


@dataclass
class Config:
    source_url: str
    source_token: str | None
    target_url: str | None
    target_token: str | None
    dry_run: bool
    rows_per_post: int
    timeout: int
    max_uncompressed: int
    gzip_body: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = env_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = env_str(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


class Masker:
    """Replaces secret values with [redacted] before anything is logged."""

    def __init__(self, values: list[str | None]) -> None:
        self._values = [v.strip() for v in values if v and len(v.strip()) >= 4]

    def clean(self, text: object) -> str:
        out = str(text)
        for secret in self._values:
            if secret in out:
                out = out.replace(secret, "[redacted]")
        return out


class Audit:
    """Structured JSON-lines audit log + human-readable stdout mirror."""

    def __init__(self, path: Path, masker: Masker, debug_names: bool) -> None:
        self.path = path
        self._masker = masker
        self._debug_names = debug_names
        self._fh = open(path, "w", encoding="utf-8")

    @staticmethod
    def hash_name(name: str) -> str:
        return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]

    def name(self, real: str) -> str:
        return real if self._debug_names else self.hash_name(real)

    def event(self, event: str, **fields: object) -> None:
        record = {"ts": utc_now(), "event": event, **fields}
        line = self._masker.clean(json.dumps(record, ensure_ascii=False, default=str))
        self._fh.write(line + "\n")
        self._fh.flush()
        print(self._human(event, fields), flush=True)

    @staticmethod
    def _human(event: str, fields: dict) -> str:
        label = {
            "post_begin": "manifest",
            "post_chunk": "chunk",
            "chunk_planned": "chunk(dry)",
            "post_complete": "complete",
        }.get(event, event)
        parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        return f"[{label}] {parts}".rstrip()

    def close(self) -> None:
        self._fh.close()


def retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped."""
    return min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempt - 1))) * (0.8 + 0.4 * random.random())


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS or status >= 500


def http_get_stream(
    url: str, token: str | None, dest: Path, timeout: int, audit: Audit
) -> dict:
    """Stream the archive to dest with retries. Never logs the URL."""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_error = "unknown"
    for attempt in range(1, GET_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise PipelineError(
                        f"source returned unexpected HTTP {status} "
                        "(no cached state is kept, so conditional responses cannot be used)"
                    )
                digest = hashlib.sha256()
                size = 0
                with open(dest, "wb") as out:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block)
                        digest.update(block)
                        size += len(block)
                return {
                    "final_url": response.geturl(),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if not is_retryable_status(exc.code):
                raise PipelineError(
                    f"source returned HTTP {exc.code} (fatal, not retried)"
                ) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError, OSError) as exc:
            last_error = f"network error: {exc.__class__.__name__}"
        if attempt < GET_ATTEMPTS:
            delay = retry_delay(attempt)
            audit.event("retry", phase="download", attempt=attempt,
                        next_in_s=round(delay, 2), reason=last_error)
            time.sleep(delay)
    raise PipelineError(f"download failed after {GET_ATTEMPTS} attempts ({last_error})")


def http_post_json(
    url: str,
    token: str,
    payload: dict,
    timeout: int,
    gzip_body: bool,
    what: str,
    audit: Audit,
) -> tuple[int, int]:
    """POST a JSON payload with retries. Returns (status, body_bytes)."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if gzip_body:
        body = gzip.compress(body, compresslevel=6)
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if gzip_body:
        headers["Content-Encoding"] = "gzip"
    last_error = "unknown"
    for attempt in range(1, POST_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                response.read()
                if 200 <= status < 300:
                    return status, len(body)
                raise PipelineError(f"endpoint returned unexpected HTTP {status} for {what}")
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            exc.read()
            if not is_retryable_status(exc.code):
                raise PipelineError(
                    f"endpoint rejected {what} with HTTP {exc.code} (fatal, not retried)"
                ) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError, OSError) as exc:
            last_error = f"network error: {exc.__class__.__name__}"
        if attempt < POST_ATTEMPTS:
            delay = retry_delay(attempt)
            audit.event("retry", phase="deliver", what=what, attempt=attempt,
                        next_in_s=round(delay, 2), reason=last_error)
            time.sleep(delay)
    raise PipelineError(f"delivery of {what} failed after {POST_ATTEMPTS} attempts ({last_error})")


def extract_archive(archive: Path, dest: Path, max_bytes: int, audit: Audit) -> list[Path]:
    """Unzip tables (flattened) into dest, enforcing a decompression cap."""
    tables: list[Path] = []
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [info for info in zf.infolist() if not info.is_dir()]
            planned = sum(info.file_size for info in members)
            if planned > max_bytes:
                raise PipelineError(
                    f"archive would decompress to {planned} bytes, over the {max_bytes} byte cap (refusing)"
                )
            used: set[str] = set()
            skipped = 0
            for info in members:
                base = Path(info.filename).name
                if info.filename.startswith("__MACOSX/") or base.startswith("."):
                    skipped += 1
                    continue
                if Path(base).suffix.lower() not in TABLE_EXTENSIONS:
                    skipped += 1
                    continue
                if base.lower() in used:
                    raise PipelineError(f"archive contains duplicate table name {base!r}")
                used.add(base.lower())
                target = dest / base
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                tables.append(target)
    except zipfile.BadZipFile:
        raise PipelineError("source did not return a valid ZIP archive") from None
    except zipfile.LargeZipFile:
        raise PipelineError("archive requires unsupported ZIP64 features") from None
    except RuntimeError as exc:
        raise PipelineError(f"archive could not be opened ({exc})") from None
    audit.event("extract_done", members=len(members), tables=len(tables), skipped=skipped)
    return tables


def scan_table(path: Path) -> dict:
    """First pass: header + row count. Raises on malformed headers."""
    header: list[str] | None = None
    rows = 0
    ragged = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            return {"header": None, "rows": 0, "ragged": 0}
        blanks = [i + 1 for i, h in enumerate(header) if h == ""]
        if blanks:
            raise PipelineError(f"table {path.name} has blank header column(s) at positions {blanks}")
        dupes = sorted({h for h in header if header.count(h) > 1})
        if dupes:
            raise PipelineError(f"table {path.name} has duplicate header columns: {dupes}")
        for row in reader:
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if len(row) != len(header):
                ragged += 1
            rows += 1
    return {"header": header, "rows": rows, "ragged": ragged}


def iter_rows(path: Path, header: list[str]):
    """Second pass: yield each row as a dict keyed by column name.

    Short rows are padded with "", long rows truncated, so every record has
    exactly the declared columns.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        width = len(header)
        for row in reader:
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if len(row) < width:
                row = row + [""] * (width - len(row))
            elif len(row) > width:
                row = row[:width]
            yield dict(zip(header, row))


def file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def deliver_table(ds: dict, batch_id: str, cfg: Config, audit: Audit) -> dict:
    """Second pass per table: stream rows out in fixed-size JSON chunks."""
    rows_sent = 0
    chunks_sent = 0
    bytes_sent = 0
    buffer: list[dict] = []
    chunk_index = 0

    def flush() -> None:
        nonlocal chunk_index, rows_sent, chunks_sent, bytes_sent
        chunk_index += 1
        payload = {
            "schema": SCHEMA,
            "event": "chunk",
            "batch_id": batch_id,
            "dataset": ds["name"],
            "chunk_index": chunk_index,
            "chunk_count": ds["chunk_count"],
            "columns": ds["header"],
            "rows": buffer,
        }
        what = f"{ds['name']} chunk {chunk_index}/{ds['chunk_count']}"
        if cfg.dry_run:
            size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            bytes_sent += size
            audit.event("chunk_planned", table=audit.name(ds["name"]), chunk_index=chunk_index,
                        chunk_count=ds["chunk_count"], rows=len(buffer), bytes=size)
        else:
            _, sent = http_post_json(cfg.target_url, cfg.target_token, payload,
                                     cfg.timeout, cfg.gzip_body, what, audit)
            bytes_sent += sent
            audit.event("post_chunk", table=audit.name(ds["name"]), chunk_index=chunk_index,
                        chunk_count=ds["chunk_count"], rows=len(buffer), bytes=sent)
        rows_sent += len(buffer)
        chunks_sent += 1
        buffer.clear()

    for row in iter_rows(ds["path"], ds["header"]):
        buffer.append(row)
        if len(buffer) >= cfg.rows_per_post:
            flush()
    if buffer:
        flush()
    return {"rows_delivered": rows_sent, "chunks_delivered": chunks_sent, "bytes_sent": bytes_sent}


def prepare_work_dir(path: Path) -> None:
    """Wipe + recreate the scratch dir, with guards against bad paths."""
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ConfigError(f"refusing to use {path} as the scratch dir")
    try:
        if resolved == Path.home().resolve():
            raise ConfigError(f"refusing to use the home directory as the scratch dir")
        cwd = Path.cwd().resolve()
        if cwd == resolved or cwd.is_relative_to(resolved):
            raise ConfigError(f"refusing a scratch dir that contains the current directory ({path})")
    except OSError:
        pass
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def validate_config(cfg: Config) -> None:
    if not cfg.source_url.lower().startswith(("http://", "https://")):
        raise ConfigError("SOURCE_URL is missing or not an http(s) URL")
    if cfg.dry_run:
        return
    if not cfg.target_url or not cfg.target_url.lower().startswith(("http://", "https://")):
        raise ConfigError("TARGET_URL is missing or not an http(s) URL")
    if not cfg.target_token:
        raise ConfigError("TARGET_TOKEN is missing (needed to authenticate delivery requests)")


def write_step_summary(datasets: list[dict], totals: dict, batch_id: str,
                       dry_run: bool, debug_names: bool) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Delivery summary",
        "",
        f"- batch: `{batch_id}`",
        f"- mode: {'dry run (nothing sent)' if dry_run else 'delivered'}",
        f"- tables: {len(datasets)}",
        f"- rows: {totals['rows']}",
        f"- data requests: {totals['chunks']} (+ begin/complete notices)",
        f"- payload bytes: {totals['bytes']}",
        "",
        "| table | rows | chunks |",
        "| --- | ---: | ---: |",
    ]
    for ds in datasets:
        label = ds["name"] if debug_names else Audit.hash_name(ds["name"])
        lines.append(f"| {label} | {ds['rows']} | {ds['chunk_count']} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    started = time.monotonic()
    cfg = Config(
        source_url=env_str("SOURCE_URL") or "",
        source_token=env_str("SOURCE_TOKEN"),
        target_url=env_str("TARGET_URL"),
        target_token=env_str("TARGET_TOKEN"),
        dry_run=env_bool("DRY_RUN"),
        rows_per_post=env_int("ROWS_PER_POST", 10000, 1, 200000),
        timeout=env_int("HTTP_TIMEOUT", 300, 10, 900),
        max_uncompressed=env_int("MAX_UNCOMPRESSED_MB", 2048, 1, 4096) * 1024 * 1024,
        gzip_body=env_bool("TARGET_GZIP"),
    )
    debug_names = env_bool("DEBUG_NAMES")

    masker = Masker([cfg.source_url, cfg.source_token, cfg.target_url, cfg.target_token])
    audit_dir = Path(env_str("AUDIT_DIR") or "./audit-log")
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit = Audit(audit_dir / f"audit-{stamp}-{uuid.uuid4().hex[:6]}.jsonl", masker, debug_names)

    work_dir = Path(env_str("WORK_DIR") or tempfile.mkdtemp(prefix="relay-run-"))
    batch_id = uuid.uuid4().hex
    status = "failed"
    error_message: str | None = None
    try:
        validate_config(cfg)
        audit.event("run_start", schema=SCHEMA, batch_id=batch_id, dry_run=cfg.dry_run,
                    rows_per_post=cfg.rows_per_post, timeout_s=cfg.timeout,
                    gzip=cfg.gzip_body, work_dir=str(work_dir))
        prepare_work_dir(work_dir)

        archive_path = work_dir / "archive.zip"
        audit.event("download_start")
        dl = http_get_stream(cfg.source_url, cfg.source_token, archive_path, cfg.timeout, audit)
        if dl["bytes"] == 0:
            raise PipelineError("source returned an empty response (0 bytes)")
        audit.event("download_done", bytes=dl["bytes"], sha256=dl["sha256"],
                    etag=dl["etag"], last_modified=dl["last_modified"])

        tables = extract_archive(archive_path, work_dir / "tables", cfg.max_uncompressed, audit)
        if not tables:
            raise PipelineError("no .txt/.csv tables found in the archive")

        datasets: list[dict] = []
        for table in tables:
            scan = scan_table(table)
            if scan["header"] is None:
                audit.event("table_skipped", table=audit.name(table.stem),
                            file=table.name, reason="empty file")
                continue
            chunk_count = (scan["rows"] + cfg.rows_per_post - 1) // cfg.rows_per_post
            digest, size = file_digest(table)
            datasets.append({
                "name": table.stem,
                "file": table.name,
                "path": table,
                "header": scan["header"],
                "rows": scan["rows"],
                "ragged": scan["ragged"],
                "bytes": size,
                "sha256": digest,
                "chunk_count": chunk_count,
            })
            audit.event("table_counted", table=audit.name(table.stem), rows=scan["rows"],
                        chunks=chunk_count, columns=len(scan["header"]), ragged=scan["ragged"])
        if not datasets:
            raise PipelineError("archive contained no parseable tables")

        manifest = {
            "schema": SCHEMA,
            "event": "begin",
            "batch_id": batch_id,
            "generated_at": utc_now(),
            "dry_run": cfg.dry_run,
            "rows_per_post": cfg.rows_per_post,
            "source": {
                "url": cfg.source_url,
                "final_url": dl["final_url"],
                "etag": dl["etag"],
                "last_modified": dl["last_modified"],
                "bytes": dl["bytes"],
                "sha256": dl["sha256"],
            },
            "datasets": [
                {k: ds[k] for k in ("name", "file", "rows", "bytes", "sha256", "chunk_count")}
                for ds in datasets
            ],
        }
        if not cfg.dry_run:
            http_post_json(cfg.target_url, cfg.target_token, manifest,
                           cfg.timeout, cfg.gzip_body, "begin manifest", audit)
        audit.event("post_begin", datasets=len(datasets),
                    rows_total=sum(d["rows"] for d in datasets), dry_run=cfg.dry_run)

        totals = {"rows": 0, "chunks": 0, "bytes": 0}
        for ds in datasets:
            result = deliver_table(ds, batch_id, cfg, audit)
            totals["rows"] += result["rows_delivered"]
            totals["chunks"] += result["chunks_delivered"]
            totals["bytes"] += result["bytes_sent"]
            audit.event("table_delivered", table=audit.name(ds["name"]),
                        rows=result["rows_delivered"], chunks=result["chunks_delivered"],
                        dry_run=cfg.dry_run)

        completion = {
            "schema": SCHEMA,
            "event": "complete",
            "batch_id": batch_id,
            "status": "ok",
            "dry_run": cfg.dry_run,
            "datasets": len(datasets),
            "rows_delivered": totals["rows"],
            "chunks_delivered": totals["chunks"],
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }
        if not cfg.dry_run:
            http_post_json(cfg.target_url, cfg.target_token, completion,
                           cfg.timeout, cfg.gzip_body, "completion notice", audit)
        audit.event("post_complete", datasets=completion["datasets"],
                    rows_delivered=completion["rows_delivered"],
                    chunks_delivered=completion["chunks_delivered"],
                    elapsed_seconds=completion["elapsed_seconds"],
                    bytes_sent=totals["bytes"], dry_run=cfg.dry_run)

        status = "ok"
        write_step_summary(datasets, totals, batch_id, cfg.dry_run, debug_names)
        print(
            f"::notice::done: {len(datasets)} tables, {totals['rows']} rows, "
            f"{totals['chunks']} requests, {round(time.monotonic() - started, 1)}s "
            f"(dry_run={cfg.dry_run})",
            flush=True,
        )
        return 0
    except PipelineError as exc:
        error_message = masker.clean(str(exc))
        audit.event("error", message=error_message)
        print(f"::error::{error_message}", flush=True)
    except Exception as exc:  # unexpected bug — still audited, still a clean exit
        error_message = masker.clean(f"{exc.__class__.__name__}: {exc}")
        audit.event("error", message=error_message, traceback=traceback.format_exc())
        traceback.print_exc()
        print(f"::error::{error_message}", flush=True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        audit.event("run_summary", status=status, error=error_message,
                    elapsed_seconds=round(time.monotonic() - started, 1),
                    audit_file=str(audit.path))
        audit.event("cleanup_done", work_dir=str(work_dir), removed=True)
        audit.close()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("::error::interrupted", flush=True)
        sys.exit(130)
