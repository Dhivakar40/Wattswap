"""Prometheus metrics."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "wattswap_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram("wattswap_http_request_seconds", "HTTP latency", ["method", "route"])
READINGS_INGESTED = Counter(
    "wattswap_readings_ingested_total", "Meter readings seen by ingest", ["result"]
)
MINTS = Counter("wattswap_mints_total", "Mint pipeline outcomes", ["status"])
MINTS_PENDING = Gauge("wattswap_mints_pending", "Mints waiting for or in flight to the chain")
