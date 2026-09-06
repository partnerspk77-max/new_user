"""
ArcGIS REST Feature Client for Miami-Dade Property Appraiser (PaGISView).
Fetches parcel data, structural attributes, building year built, square footage, and valuation.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from src.enrichment.models import PropertyParcel
from src.logger import logger


DEFAULT_PAGIS_URL = (
    "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/PaGISView_gdb/FeatureServer/0/query"
)

PARCEL_OUT_FIELDS = [
    "FOLIO",
    "TRUE_SITE_ADDR",
    "TRUE_SITE_ZIP_CODE",
    "TRUE_MAILING_ADDR1",
    "TRUE_MAILING_ADDR2",
    "TRUE_MAILING_ZIP_CODE",
    "TRUE_OWNER1",
    "DOR_CODE_CUR",
    "DOR_DESC",
    "BEDROOM_COUNT",
    "BATHROOM_COUNT",
    "BUILDING_ACTUAL_AREA",
    "BUILDING_HEATED_AREA",
    "YEAR_BUILT",
    "ASSESSED_VAL_CUR",
]


class ParcelClient:
    """Production client for Miami-Dade Property Appraiser GIS Feature Service."""

    def __init__(
        self,
        base_url: str = DEFAULT_PAGIS_URL,
        batch_size: int = 100,
        timeout: int = 20,
        request_delay: float = 0.15,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url
        self.batch_size = max(1, min(batch_size, 200))
        self.timeout = timeout
        self.request_delay = request_delay
        self.session = session or self._create_session()

    def _create_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": "MiamiDadePermitIntelligence/2.0 (+https://github.com/balmen)",
                "Accept": "application/json",
            }
        )
        retries = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def fetch_parcels_batch(self, folios: List[str]) -> List[PropertyParcel]:
        """
        Fetches parcel details for a batch of folios (up to batch_size).
        Normalizes folios by stripping hyphens and whitespace.
        """
        cleaned_folios = list({f.replace("-", "").strip() for f in folios if f and f.strip()})
        if not cleaned_folios:
            return []

        formatted_list = ", ".join(f"'{f}'" for f in cleaned_folios)
        where_clause = f"FOLIO IN ({formatted_list})"

        params = {
            "where": where_clause,
            "outFields": ",".join(PARCEL_OUT_FIELDS),
            "returnGeometry": "false",
            "f": "json",
        }

        try:
            resp = self.session.post(self.base_url, data=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                err_msg = data["error"].get("message", "Unknown ArcGIS error")
                logger.error(f"ArcGIS PaGISView error for batch: {err_msg}")
                return []

            features = data.get("features", [])
            parcels: List[PropertyParcel] = []
            for feat in features:
                attrs = feat.get("attributes", {})
                if attrs.get("FOLIO"):
                    parcels.append(PropertyParcel.from_arcgis_attributes(attrs))

            return parcels

        except Exception as exc:
            logger.error(f"Failed to fetch parcel batch of {len(cleaned_folios)} folios: {exc}")
            return []

    def fetch_all_parcels(
        self,
        folios: List[str],
        progress_callback: Optional[Any] = None,
    ) -> List[PropertyParcel]:
        """
        Iterates over all requested folios in chunked batches with delay and error resilience.
        """
        unique_folios = list({f.replace("-", "").strip() for f in folios if f and f.strip()})
        total = len(unique_folios)
        results: List[PropertyParcel] = []

        logger.info(f"Starting parcel enrichment for {total} unique folios in chunks of {self.batch_size}...")

        for i in range(0, total, self.batch_size):
            chunk = unique_folios[i : i + self.batch_size]
            parcels = self.fetch_parcels_batch(chunk)
            results.extend(parcels)

            if progress_callback:
                progress_callback(len(results), total)

            if i + self.batch_size < total and self.request_delay > 0:
                time.sleep(self.request_delay)

        logger.info(f"Enrichment complete. Successfully retrieved {len(results)} of {total} parcels.")
        return results
