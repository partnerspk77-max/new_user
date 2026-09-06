"""
ArcGIS REST API Client for Miami-Dade County Building Permits.
Handles:
- Resilient HTTP requests with exponential backoff and jitter
- Automatic spatial reference transformation (outSR=4326 for WGS84 coordinates)
- Strict field whitelisting for production efficiency
- Deterministic ordering and pagination
- ArcGIS server-side statistics queries
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from src.client.exceptions import (
    ArcGISClientError,
    ArcGISError,
    ArcGISRateLimitError,
    ArcGISServerError,
    ArcGISTimeoutError,
)
from src.config import config
from src.logger import logger


PRODUCTION_FIELDS = [
    "OBJECTID",
    "FOLIO",
    "ID",
    "PROCNUM",
    "ADDRESS",
    "UNIT",
    "ISCONDO",
    "TYPE",
    "CAT1",
    "DESC1",
    "CAT2",
    "DESC2",
    "CAT3",
    "DESC3",
    "CAT4",
    "DESC4",
    "CAT5",
    "DESC5",
    "CAT6",
    "DESC6",
    "CAT7",
    "DESC7",
    "CAT8",
    "DESC8",
    "CAT9",
    "DESC9",
    "CAT10",
    "DESC10",
    "ISSUDATE",
    "LSTINSDT",
    "RENDATE",
    "BLDCMPDT",
    "RESCOMM",
    "PROPUSE",
    "APPTYPE",
    "FFRMLINE",
    "MPRMTNUM",
    "LSTAPPRDT",
    "CONTRNUM",
    "CONTRNAME",
    "BPSTATUS",
    "GlobalID",
]


class ArcGISClient:
    """Enterprise client for Miami-Dade ArcGIS REST Feature Layer."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        page_size: Optional[int] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        backoff_factor: Optional[float] = None,
        request_delay: Optional[float] = None,
        api_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url or config.api_url).rstrip("/")
        self.page_size = page_size or config.page_size
        self.timeout = timeout or config.request_timeout
        self.max_retries = max_retries or config.max_retries
        self.backoff_factor = backoff_factor or config.retry_backoff_factor
        self.request_delay = request_delay if request_delay is not None else config.request_delay_seconds
        self.api_token = api_token or config.api_token

        self.session = session or self._create_session()

    def _create_session(self) -> requests.Session:
        s = requests.Session()
        headers = {
            "User-Agent": config.user_agent,
            "Accept": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        s.headers.update(headers)
        return s

    @staticmethod
    def _sanitize_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Removes sensitive keys before logging."""
        sanitized = {}
        for k, v in params.items():
            if any(s in k.lower() for s in ("token", "secret", "key", "password", "auth")):
                sanitized[k] = "***REDACTED***"
            else:
                sanitized[k] = v
        return sanitized

    def _execute_request(self, params: Dict[str, Any], page_number: int = 1) -> Dict[str, Any]:
        """
        Executes HTTP request with exponential backoff, jitter, and structured logging.
        """
        attempt = 0
        last_exception: Optional[Exception] = None
        # Add token to params if required by ArcGIS server
        active_params = dict(params)
        if self.api_token and "token" not in active_params:
            active_params["token"] = self.api_token

        while attempt < self.max_retries:
            attempt += 1
            start_time = time.perf_counter()

            # Sanitize endpoint and params before logging
            safe_params = self._sanitize_params(active_params)

            logger.log_event(
                "source_requested",
                endpoint=self.base_url,
                page_number=page_number,
                attempt=attempt,
                where=safe_params.get("where"),
                offset=safe_params.get("resultOffset"),
            )

            try:
                # Polite rate limiting between requests
                if self.request_delay > 0 and attempt == 1:
                    time.sleep(self.request_delay)

                response = self.session.get(
                    self.base_url,
                    params=active_params,
                    timeout=self.timeout,
                )
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

                # Check for rate limiting
                if response.status_code == 429:
                    raise ArcGISRateLimitError(f"Rate limit exceeded (HTTP 429) from {self.base_url}")

                # Check for server errors
                if 500 <= response.status_code < 600:
                    raise ArcGISServerError(f"Server error (HTTP {response.status_code}) from {self.base_url}")

                # Check for client errors
                if 400 <= response.status_code < 500:
                    raise ArcGISClientError(f"Client error (HTTP {response.status_code}): {response.text[:200]}")

                response.raise_for_status()
                data = response.json()

                # ArcGIS error embedded in 200 OK response
                if "error" in data:
                    err = data["error"]
                    code = err.get("code")
                    msg = err.get("message", "Unknown ArcGIS error")
                    details = err.get("details", [])
                    raise ArcGISError(f"ArcGIS Error {code}: {msg} | Details: {details}")

                features_count = len(data.get("features", [])) if "features" in data else 0

                logger.log_event(
                    "page_fetched",
                    page_number=page_number,
                    duration_ms=duration_ms,
                    http_status=response.status_code,
                    records_received=features_count,
                    has_exceeded_transfer_limit=data.get("exceededTransferLimit", False),
                )

                return data

            except (requests.exceptions.Timeout, requests.exceptions.ConnectTimeout) as e:
                last_exception = ArcGISTimeoutError(f"Request timed out after {self.timeout}s: {e}")
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
                last_exception = ArcGISError(f"Connection failed: {e}")
            except (ArcGISRateLimitError, ArcGISServerError) as e:
                last_exception = e
            except ArcGISClientError:
                # Do not retry 4xx errors
                raise
            except Exception as e:
                last_exception = ArcGISError(f"Unexpected request error: {e}")

            # Calculate exponential backoff with jitter
            backoff = (self.backoff_factor ** attempt) + random.uniform(0.1, 0.5)
            logger.warning(
                f"ArcGIS request attempt {attempt}/{self.max_retries} failed. Retrying in {backoff:.2f}s...",
                context={
                    "attempt": attempt,
                    "max_retries": self.max_retries,
                    "error": str(last_exception),
                    "backoff_seconds": round(backoff, 2),
                },
            )
            time.sleep(backoff)

        raise last_exception or ArcGISError("Maximum retry attempts reached without response.")

    def query_records(
        self,
        where: str = "1=1",
        offset: int = 0,
        limit: Optional[int] = None,
        order_by: str = "ISSUDATE ASC, OBJECTID ASC",
        out_fields: Optional[List[str]] = None,
        return_geometry: bool = True,
        page_number: int = 1,
    ) -> Dict[str, Any]:
        """
        Queries building permits using ArcGIS REST pagination & WGS84 spatial reference.
        """
        record_count = limit if limit is not None else self.page_size
        fields_str = ",".join(out_fields) if out_fields else ",".join(PRODUCTION_FIELDS)

        params: Dict[str, Any] = {
            "where": where,
            "outFields": fields_str,
            "returnGeometry": "true" if return_geometry else "false",
            "outSR": "4326",  # Request WGS84 (lat/lon) directly from server
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": record_count,
            "orderByFields": order_by,
        }

        return self._execute_request(params, page_number=page_number)

    def query_count(self, where: str = "1=1") -> int:
        """Returns total record count matching a WHERE clause using returnCountOnly=true."""
        params = {
            "where": where,
            "returnCountOnly": "true",
            "f": "json",
        }
        res = self._execute_request(params)
        return int(res.get("count", 0))

    def query_statistics(
        self,
        where: str = "1=1",
        out_statistics: Optional[List[Dict[str, Any]]] = None,
        group_by_fields: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Executes server-side statistics queries if supported by the service."""
        import json
        params: Dict[str, Any] = {
            "where": where,
            "f": "json",
        }
        if out_statistics:
            params["outStatistics"] = json.dumps(out_statistics)
        if group_by_fields:
            params["groupByFieldsForStatistics"] = group_by_fields

        res = self._execute_request(params)
        return res.get("features", [])
