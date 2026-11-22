#!/usr/bin/env python3
"""archive-relay (snapshot mode) — fetch one live JSON document, relay it.

Flow: GET SNAPSHOT_URL -> validate the JSON -> wrap it in a dated envelope ->
POST the whole thing to TARGET_URL as a single request.

- Standard library only (nothing to install).
- Triggered by repo pushes or manually; this script does no scheduling.
- Nothing touches disk except the audit log: no temp files, no caches,
  nothing kept between runs.
- Every outbound request is retried with exponential backoff and jitter;
  any 2xx response counts as delivered.
- Works with any JSON source — the URL lives in secrets, never in code.

Configuration (environment):
  SNAPSHOT_URL     (required unless DRY_RUN) URL of the live JSON document
  SNAPSHOT_TOKEN   (optional) bearer token for the source
  TARGET_URL       (required unless DRY_RUN) delivery endpoint
  TARGET_TOKEN     (required unless DRY_RUN) bearer token for the endpoint
  TARGET_GZIP      "true" to gzip the request body (Content-Encoding: gzip)
  DRY_RUN          "true" to fetch and parse only, no delivery request
  HTTP_TIMEOUT     per-request timeout in seconds (default 120)
  MAX_PAYLOAD_MB   cap on the response size (default 50)
  AUDIT_DIR        audit log directory (default ./audit-log); not wiped.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import os
import random
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "archive-relay/v1"
# Source policy: requests must carry a User-Agent that identifies the project
# (requests without one get blocked). Keep the +URL so the operator can
# reach the project page. Update it if the site moves.
USER_AGENT = "USA-Live-Trains/1.0 (+https://usa.metro-status.com)"
GET_ATTEMPTS = 4
POST_ATTEMPTS = 5
RETRY_BASE_SECONDS = 2.0
RETRY_CAP_SECONDS = 30.0
RETRYABLE_STATUS = {408, 429}
BLOCK = 1024 * 1024


class PipelineError(Exception):
    """Any expected failure (config, source, delivery)."""


class ConfigError(PipelineError):
    """Bad or missing configuration."""


@dataclass
class Config:
    snapshot_url: str
    snapshot_token: str | None
    target_url: str | None
    target_token: str | None
    dry_run: bool
    timeout: int
    max_bytes: int
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

    def __init__(self, path: Path, masker: Masker) -> None:
        self.path = path
        self._masker = masker
        self._fh = open(path, "w", encoding="utf-8")

    def event(self, event: str, **fields: object) -> None:
        record = {"ts": utc_now(), "event": event, **fields}
        line = self._masker.clean(json.dumps(record, ensure_ascii=False, default=str))
        self._fh.write(line + "\n")
        self._fh.flush()
        print(self._human(event, fields), flush=True)

    @staticmethod
    def _human(event: str, fields: dict) -> str:
        label = {
            "fetch_start": "fetch",
            "fetch_done": "fetched",
            "parse_done": "parsed",
            "snapshot_planned": "snapshot(dry)",
            "post_snapshot": "snapshot",
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


def http_get_json(
    url: str, token: str | None, timeout: int, max_bytes: int, audit: Audit
) -> dict:
    """GET the document with retries, enforce a size cap, parse the JSON.

    Never logs the URL. Raises PipelineError on a fatal HTTP status, an
    oversized response, or a body that is not valid JSON.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, */*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_error = "unknown"
    for attempt in range(1, GET_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise PipelineError(f"source returned unexpected HTTP {status}")
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise PipelineError(
                        f"response is {declared} bytes, over the {max_bytes} byte cap (refusing)"
                    )
                digest = hashlib.sha256()
                size = 0
                blocks: list[bytes] = []
                while True:
                    block = response.read(BLOCK)
                    if not block:
                        break
                    size += len(block)
                    if size > max_bytes:
                        raise PipelineError(
                            f"response exceeded the {max_bytes} byte cap mid-download (refusing)"
                        )
                    digest.update(block)
                    blocks.append(block)
                meta = {
                    "final_url": response.geturl(),
                    "content_type": response.headers.get("Content-Type"),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
            raw = b"".join(blocks)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise PipelineError(
                    "source did not return valid JSON "
                    f"(got {size} bytes of {meta['content_type'] or 'unknown type'})"
                ) from None
            return {"data": data, "bytes": size, "sha256": digest.hexdigest(), **meta}
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            exc.read()
            if not is_retryable_status(exc.code):
                raise PipelineError(
                    f"source returned HTTP {exc.code} (fatal, not retried)"
                ) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError, OSError) as exc:
            last_error = f"network error: {exc.__class__.__name__}"
        if attempt < GET_ATTEMPTS:
            delay = retry_delay(attempt)
            audit.event("retry", phase="fetch", attempt=attempt,
                        next_in_s=round(delay, 2), reason=last_error)
            time.sleep(delay)
    raise PipelineError(f"fetch failed after {GET_ATTEMPTS} attempts ({last_error})")


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


def summarize(data: object) -> dict:
    """Small generic size hint for the receiver: keys and/or item count."""
    if isinstance(data, dict):
        lists = [len(v) for v in data.values() if isinstance(v, list)]
        return {"top_level_keys": len(data),
                "items": sum(lists) if lists else None}
    if isinstance(data, list):
        return {"top_level_keys": None, "items": len(data)}
    return {"top_level_keys": None, "items": None}


def validate_config(cfg: Config) -> None:
    if not cfg.snapshot_url.lower().startswith(("http://", "https://")):
        raise ConfigError("SNAPSHOT_URL is missing or not an http(s) URL")
    if cfg.dry_run:
        return
    if not cfg.target_url or not cfg.target_url.lower().startswith(("http://", "https://")):
        raise ConfigError("TARGET_URL is missing or not an http(s) URL")
    if not cfg.target_token:
        raise ConfigError("TARGET_TOKEN is missing (needed to authenticate the delivery request)")


def main() -> int:
    started = time.monotonic()
    cfg = Config(
        snapshot_url=env_str("SNAPSHOT_URL") or "",
        snapshot_token=env_str("SNAPSHOT_TOKEN"),
        target_url=env_str("TARGET_URL"),
        target_token=env_str("TARGET_TOKEN"),
        dry_run=env_bool("DRY_RUN"),
        timeout=env_int("HTTP_TIMEOUT", 120, 10, 900),
        max_bytes=env_int("MAX_PAYLOAD_MB", 50, 1, 512) * 1024 * 1024,
        gzip_body=env_bool("TARGET_GZIP"),
    )
    masker = Masker([cfg.snapshot_url, cfg.snapshot_token, cfg.target_url, cfg.target_token])
    audit_dir = Path(env_str("AUDIT_DIR") or "./audit-log")
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit = Audit(audit_dir / f"snapshot-{stamp}-{uuid.uuid4().hex[:6]}.jsonl", masker)

    batch_id = uuid.uuid4().hex
    status = "failed"
    error_message: str | None = None
    try:
        validate_config(cfg)
        audit.event("run_start", schema=SCHEMA, batch_id=batch_id, dry_run=cfg.dry_run,
                    timeout_s=cfg.timeout, gzip=cfg.gzip_body,
                    max_mb=cfg.max_bytes // (1024 * 1024))

        audit.event("fetch_start")
        doc = http_get_json(cfg.snapshot_url, cfg.snapshot_token, cfg.timeout,
                            cfg.max_bytes, audit)
        audit.event("fetch_done", bytes=doc["bytes"], sha256=doc["sha256"],
                    content_type=doc["content_type"], etag=doc["etag"])

        data = doc["data"]
        if data == {} or data == []:
            raise PipelineError("source returned an empty JSON document")
        summary = summarize(data)
        audit.event("parse_done", **summary)

        envelope = {
            "schema": SCHEMA,
            "event": "snapshot",
            "batch_id": batch_id,
            "captured_at": utc_now(),
            "dry_run": cfg.dry_run,
            "source": {
                "url": cfg.snapshot_url,
                "final_url": doc["final_url"],
                "content_type": doc["content_type"],
                "etag": doc["etag"],
                "last_modified": doc["last_modified"],
                "bytes": doc["bytes"],
                "sha256": doc["sha256"],
            },
            "summary": summary,
            "payload": data,
        }
        envelope_bytes = len(json.dumps(envelope, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8"))
        if cfg.dry_run:
            audit.event("snapshot_planned", envelope_bytes=envelope_bytes)
        else:
            _, sent = http_post_json(cfg.target_url, cfg.target_token, envelope,
                                     cfg.timeout, cfg.gzip_body, "snapshot", audit)
            audit.event("post_snapshot", envelope_bytes=envelope_bytes, bytes=sent)

        status = "ok"
        parts = []
        if summary["top_level_keys"] is not None:
            parts.append(f"{summary['top_level_keys']} groups")
        if summary["items"] is not None:
            parts.append(f"{summary['items']} items")
        print(
            "::notice::snapshot relayed: " + ", ".join(parts)
            + f", {round(time.monotonic() - started, 1)}s (dry_run={cfg.dry_run})",
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
        audit.event("run_summary", status=status, error=error_message,
                    elapsed_seconds=round(time.monotonic() - started, 1),
                    audit_file=str(audit.path))
        audit.close()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("::error::interrupted", flush=True)
        sys.exit(130)
