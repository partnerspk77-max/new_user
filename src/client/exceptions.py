"""
Custom exceptions for ArcGIS client and Ingestion Pipeline.
"""

class PipelineError(Exception):
    """Base exception for all pipeline errors."""
    pass


class ArcGISError(PipelineError):
    """Base exception for ArcGIS API issues."""
    pass


class ArcGISTimeoutError(ArcGISError):
    """Raised when request to ArcGIS times out."""
    pass


class ArcGISRateLimitError(ArcGISError):
    """Raised when HTTP 429 rate limit is encountered."""
    pass


class ArcGISServerError(ArcGISError):
    """Raised on 5xx server errors from ArcGIS."""
    pass


class ArcGISClientError(ArcGISError):
    """Raised on 4xx client errors (malformed query, etc.)."""
    pass


class ValidationError(PipelineError):
    """Raised when record fails validation."""
    pass


class ParcelFetchError(PipelineError):
    """Raised when a parcel enrichment batch fails (network, protocol, or ArcGIS error)."""
    pass


class WeatherFetchError(PipelineError):
    """Raised when NOAA/NWS storm report ingestion fails (network or protocol error)."""
    pass


class StorageError(PipelineError):
    """Raised when database or file storage encounters a failure."""
    pass
