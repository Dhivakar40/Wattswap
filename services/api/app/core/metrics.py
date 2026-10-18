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

# --- pricing engine ---------------------------------------------------------------------
PRICE = Gauge("wattswap_price_microusdc", "Applied price, mUSDC base units per kWh", ["market"])
PRICE_RAW = Gauge("wattswap_price_raw_microusdc", "Raw (pre-EMA, pre-limit) price", ["market"])
PRICE_VOL = Gauge(
    "wattswap_price_realized_vol_bps", "Realized volatility, bps per sqrt(hour)", ["market"]
)
BREAKER_STATE = Gauge(
    "wattswap_breaker_state", "0 NORMAL, 1 ANOMALY, 2 FROZEN, 3 RECOVERING", ["market"]
)
WEATHER_AGE = Gauge("wattswap_weather_age_seconds", "Age of the oldest weather in use", ["market"])
PRICING_TICKS = Counter("wattswap_pricing_ticks_total", "Pricing ticks produced", ["market"])
PRICING_EVENTS = Counter("wattswap_pricing_events_total", "Breaker and anomaly events", ["kind"])
ORACLE_PUSHES = Counter("wattswap_oracle_pushes_total", "PriceOracle updates", ["result"])
WEATHER_REQUESTS = Counter("wattswap_weather_requests_total", "Open-Meteo requests", ["result"])
