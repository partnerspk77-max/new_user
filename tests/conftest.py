"""
Shared fixtures and test helpers.
"""

import pytest
from src.storage.database import Database
from src.storage.permit_store import PermitStore
from src.storage.raw_store import RawStore
from src.storage.state_store import StateStore


@pytest.fixture
def test_db():
    """Provides a fresh in-memory SQLite database initialized with schema."""
    db = Database("sqlite://:memory:")
    db.initialize_schema()
    yield db
    db.close()


@pytest.fixture
def raw_store(test_db):
    return RawStore(test_db)


@pytest.fixture
def permit_store(test_db):
    return PermitStore(test_db)


@pytest.fixture
def state_store(test_db):
    return StateStore(test_db)


@pytest.fixture
def sample_arcgis_feature():
    """Standard valid ArcGIS feature with point geometry."""
    return {
        "attributes": {
            "OBJECTID": 1001,
            "FOLIO": "30-4015-001-0020",
            "ID": "20240101001",
            "PROCNUM": "M2024001",
            "ADDRESS": "123 BISCAYNE BLVD",
            "UNIT": "STE 400",
            "ISCONDO": "N",
            "TYPE": "BUILDING",
            "CAT1": "01",
            "DESC1": "ROOFING - TILE",
            "CAT2": "02",
            "DESC2": "ELECTRICAL",
            "CAT3": None,
            "DESC3": None,
            "CAT4": None,
            "DESC4": None,
            "CAT5": None,
            "DESC5": None,
            "CAT6": None,
            "DESC6": None,
            "CAT7": None,
            "DESC7": None,
            "CAT8": None,
            "DESC8": None,
            "CAT9": None,
            "DESC9": None,
            "CAT10": None,
            "DESC10": None,
            "ISSUDATE": 1704067200000,  # 2024-01-01 00:00:00 UTC
            "LSTINSDT": 1704672000000,  # 2024-01-08 00:00:00 UTC
            "RENDATE": None,
            "BLDCMPDT": None,
            "RESCOMM": "COMMERCIAL",
            "PROPUSE": "OFFICE",
            "APPTYPE": "NEW",
            "FFRMLINE": "ROOF REPLACEMENT AND UPGRADES",
            "MPRMTNUM": "M2024000",
            "LSTAPPRDT": 1704153600000,
            "CONTRNUM": "CGC1500000",
            "CONTRNAME": "SOUTH FLORIDA ROOFING INC",
            "BPSTATUS": "A",
            "GlobalID": "{ABC-123-XYZ}",
        },
        "geometry": {
            "x": -80.1918,  # Longitude
            "y": 25.7617,   # Latitude
        },
    }
