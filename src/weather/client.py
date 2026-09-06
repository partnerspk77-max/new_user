"""
NOAA & National Weather Service (NWS) Client.
Ingests Local Storm Reports (LSR) and severe convective weather events for Miami-Dade County.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from src.client.exceptions import WeatherFetchError
from src.logger import logger
from src.weather.models import StormEvent


DEFAULT_LSR_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"
DEFAULT_NWS_ALERTS_URL = "https://api.weather.gov/alerts"

ROOFING_STORM_TYPES = {
    "HAIL",
    "TSTM WND DMG",
    "TSTM WND GST",
    "TORNADO",
    "TROPICAL CYCLONE",
    "NON-TSTM WND GST",
    "HIGH WIND",
}


class NOAAStormClient:
    """Client for retrieving NOAA/NWS severe storm reports and weather hazards."""

    def __init__(
        self,
        base_url: str = DEFAULT_LSR_URL,
        wfo: str = "MFL",  # Miami Weather Forecast Office
        timeout: int = 25,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url
        self.wfo = wfo
        self.timeout = timeout
        self.session = session or self._create_session()

    def _create_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": "MiamiDadePermitIntelligence/2.0 (weather@balmen.com)",
                "Accept": "application/json",
            }
        )
        retries = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def fetch_local_storm_reports(
        self,
        start_date: str = "2024-01-01T00:00Z",
        end_date: Optional[str] = None,
    ) -> List[StormEvent]:
        """
        Fetches official NWS Local Storm Reports for the Miami WFO (MFL).
        Filters to events within Miami-Dade or immediate bordering coordinates.
        """
        end_date = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59Z")
        params = {
            "wfos": self.wfo,
            "sts": start_date,
            "ets": end_date,
        }

        logger.info(f"Querying NWS Local Storm Reports for WFO {self.wfo} from {start_date} to {end_date}...")

        try:
            resp = self.session.get(self.base_url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()

            features = data.get("features", [])
            events: List[StormEvent] = []

            for feat in features:
                props = feat.get("properties", {})
                county = str(props.get("county") or "").strip()
                ttype = str(props.get("typetext") or "").strip().upper()

                # Include Miami-Dade reports or relevant cross-border convective cells
                if county == "Miami-Dade" or (ttype in ROOFING_STORM_TYPES and county in ("Broward", "Monroe")):
                    ev = StormEvent.from_nws_lsr_feature(feat)
                    if ev:
                        events.append(ev)

            logger.info(f"Retrieved {len(events)} relevant severe storm events for South Florida / Miami-Dade.")
            return events

        except Exception as exc:
            # Surface the failure instead of masquerading as "no storms":
            # a silent empty list would quietly degrade storm evidence everywhere.
            raise WeatherFetchError(
                f"Failed to fetch NOAA/NWS storm reports from {self.base_url}: {exc}. "
                f"Check network connectivity; storm enrichment can be re-run safely (results are cached)."
            ) from exc
