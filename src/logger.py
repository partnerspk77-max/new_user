"""
Structured logging module for Miami-Dade Permit Pipeline.
Adheres to enterprise standards:
- File-based logging with explicit separation:
  * logs/app.log   -> INFO and above
  * logs/warn.log  -> WARN only
  * logs/error.log -> ERROR and CRITICAL only
- Structured JSON-compatible and human-readable context.
- Execution event tracking (job_started, source_requested, page_fetched, etc.).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import config


class LevelFilter(logging.Filter):
    """Filters records to include only those within a specified level range."""

    def __init__(self, exact_level: Optional[int] = None, min_level: Optional[int] = None, max_level: Optional[int] = None):
        super().__init__()
        self.exact_level = exact_level
        self.min_level = min_level
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        if self.exact_level is not None and record.levelno != self.exact_level:
            return False
        if self.min_level is not None and record.levelno < self.min_level:
            return False
        if self.max_level is not None and record.levelno > self.max_level:
            return False
        return True


class StructuredFormatter(logging.Formatter):
    """Formats log records as structured key-value / JSON strings."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()
        base_payload: Dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add structured context if attached to record
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            base_payload.update(context)

        if record.exc_info:
            base_payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(base_payload)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for console output."""

    COLORS = {
        logging.DEBUG: "\033[36m",    # Cyan
        logging.INFO: "\033[32m",     # Green
        logging.WARNING: "\033[33m",  # Yellow
        logging.ERROR: "\033[31m",    # Red
        logging.CRITICAL: "\033[35m", # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, self.RESET)
        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = f"{color}[{record.levelname:<7}]{self.RESET} {time_str} - {record.name}: {record.getMessage()}"
        
        context = getattr(record, "context", None)
        if isinstance(context, dict) and context:
            ctx_items = " ".join(f"{k}={v}" for k, v in context.items())
            msg = f"{msg} | {ctx_items}"

        if record.exc_info:
            msg += f"\n{self.formatException(record.exc_info)}"
        return msg


def setup_logging(log_dir: Optional[Path] = None, log_level: Optional[str] = None) -> logging.Logger:
    """Configures structured file-separated logging and returns root application logger."""
    active_log_dir = log_dir or config.log_dir
    active_log_level_name = log_level or config.log_level
    active_log_level = getattr(logging, active_log_level_name, logging.INFO)

    active_log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Clear existing handlers to prevent duplicates during reconfiguration/testing
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    json_formatter = StructuredFormatter()
    console_formatter = ConsoleFormatter()

    # 1. Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(active_log_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 2. app.log Handler (INFO and above)
    app_log_path = active_log_dir / "app.log"
    app_handler = logging.FileHandler(app_log_path, encoding="utf-8")
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(json_formatter)
    root_logger.addHandler(app_handler)

    # 3. warn.log Handler (WARNING only)
    warn_log_path = active_log_dir / "warn.log"
    warn_handler = logging.FileHandler(warn_log_path, encoding="utf-8")
    warn_handler.setLevel(logging.WARNING)
    warn_handler.addFilter(LevelFilter(exact_level=logging.WARNING))
    warn_handler.setFormatter(json_formatter)
    root_logger.addHandler(warn_handler)

    # 4. error.log Handler (ERROR and CRITICAL only)
    error_log_path = active_log_dir / "error.log"
    error_handler = logging.FileHandler(error_log_path, encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(json_formatter)
    root_logger.addHandler(error_handler)

    return logging.getLogger("miami_dade_pipeline")


class PipelineLogger:
    """High-level structured logger wrapper for pipeline events."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def log_event(self, event_name: str, level: int = logging.INFO, **context: Any) -> None:
        """Logs a lifecycle event with structured context."""
        payload = {"event": event_name, **context}
        self._logger.log(level, f"Event: {event_name}", extra={"context": payload})

    def info(self, message: str, **context: Any) -> None:
        self._logger.info(message, extra={"context": context} if context else None)

    def warning(self, message: str, **context: Any) -> None:
        self._logger.warning(message, extra={"context": context} if context else None)

    def error(self, message: str, exc_info: bool = True, **context: Any) -> None:
        self._logger.error(message, exc_info=exc_info, extra={"context": context} if context else None)


# Initialize default logger
_root_logger = setup_logging()
logger = PipelineLogger(_root_logger)
