"""
Unit tests for ArcGIS REST client.
Tests resilient HTTP behavior, retry logic, exponential backoff, and query construction.
"""

import pytest
import requests
from unittest.mock import MagicMock, patch

from src.client.arcgis_client import ArcGISClient, PRODUCTION_FIELDS
from src.client.exceptions import (
    ArcGISClientError,
    ArcGISError,
    ArcGISRateLimitError,
    ArcGISServerError,
    ArcGISTimeoutError,
)


class TestArcGISClient:
    def test_query_params_structure(self):
        mock_session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"features": []}
        mock_session.get.return_value = mock_resp

        client = ArcGISClient(
            base_url="https://fake-gis.gov/query",
            page_size=500,
            session=mock_session,
            request_delay=0,
        )

        client.query_records(
            where="ISSUDATE >= TIMESTAMP '2024-01-01 00:00:00'",
            offset=1000,
            limit=500,
        )

        call_args = mock_session.get.call_args
        params = call_args[1]["params"]

        assert params["where"] == "ISSUDATE >= TIMESTAMP '2024-01-01 00:00:00'"
        assert params["outSR"] == "4326"  # Must request WGS84
        assert params["returnGeometry"] == "true"
        assert params["f"] == "json"
        assert params["resultOffset"] == 1000
        assert params["resultRecordCount"] == 500
        assert "OBJECTID" in params["outFields"]
        assert "BPSTATUS" in params["outFields"]
        assert "CAT1" in params["outFields"]
        assert "DESC10" in params["outFields"]

    def test_retry_on_429_rate_limit(self):
        mock_session = MagicMock(spec=requests.Session)

        # First request returns 429, second returns 200 OK
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Too Many Requests"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"features": [{"attributes": {"OBJECTID": 1}}]}

        mock_session.get.side_effect = [resp_429, resp_200]

        client = ArcGISClient(
            base_url="https://fake-gis.gov/query",
            max_retries=3,
            backoff_factor=0.01,
            request_delay=0,
            session=mock_session,
        )

        data = client.query_records(where="1=1")
        assert len(data["features"]) == 1
        assert mock_session.get.call_count == 2

    def test_retry_on_503_server_error(self):
        mock_session = MagicMock(spec=requests.Session)

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = "Service Unavailable"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"features": []}

        mock_session.get.side_effect = [resp_503, resp_200]

        client = ArcGISClient(
            base_url="https://fake-gis.gov/query",
            max_retries=3,
            backoff_factor=0.01,
            request_delay=0,
            session=mock_session,
        )

        data = client.query_records(where="1=1")
        assert mock_session.get.call_count == 2

    def test_timeout_retry_and_exhaustion(self):
        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.side_effect = requests.exceptions.ConnectTimeout("Timed out")

        client = ArcGISClient(
            base_url="https://fake-gis.gov/query",
            max_retries=2,
            backoff_factor=0.01,
            request_delay=0,
            session=mock_session,
        )

        with pytest.raises(ArcGISTimeoutError):
            client.query_records(where="1=1")

        assert mock_session.get.call_count == 2

    def test_client_error_400_does_not_retry(self):
        mock_session = MagicMock(spec=requests.Session)
        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.text = "Bad Request: Invalid SQL syntax"

        mock_session.get.return_value = resp_400

        client = ArcGISClient(
            base_url="https://fake-gis.gov/query",
            max_retries=3,
            backoff_factor=0.01,
            request_delay=0,
            session=mock_session,
        )

        with pytest.raises(ArcGISClientError):
            client.query_records(where="INVALID SYNTAX")

        assert mock_session.get.call_count == 1  # Should not retry 4xx

    def test_arcgis_embedded_error_handling(self):
        mock_session = MagicMock(spec=requests.Session)
        resp_200_with_error = MagicMock()
        resp_200_with_error.status_code = 200
        resp_200_with_error.json.return_value = {
            "error": {
                "code": 400,
                "message": "Cannot perform query. Invalid query parameters.",
                "details": ["Table not found"],
            }
        }
        mock_session.get.return_value = resp_200_with_error

        client = ArcGISClient(
            base_url="https://fake-gis.gov/query",
            max_retries=2,
            backoff_factor=0.01,
            request_delay=0,
            session=mock_session,
        )

        with pytest.raises(ArcGISError) as excinfo:
            client.query_records(where="1=1")
        assert "Invalid query parameters" in str(excinfo.value)
