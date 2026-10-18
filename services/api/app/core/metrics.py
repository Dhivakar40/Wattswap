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

# --- trading ----------------------------------------------------------------------------
LEADER = Gauge("wattswap_leader", "1 while this instance holds the role's leader lock", ["role"])
ORDERS = Counter("wattswap_orders_total", "Order placement and lifecycle outcomes", ["result"])
ORDERS_OPEN = Gauge("wattswap_orders_open", "Open orders on the book", ["side"])
MATCH_CYCLE = Histogram("wattswap_match_cycle_seconds", "Matcher cycle duration")
MATCHES = Counter("wattswap_matches_total", "Pairs matched")
MATCH_SKIPPED = Counter("wattswap_match_skipped_total", "Crossing pairs not matched", ["reason"])
SETTLEMENTS = Counter("wattswap_settlements_total", "Settlement state transitions", ["status"])
SETTLE_FAILURES = Counter(
    "wattswap_settle_failures_total", "Settlements that failed or were released", ["reason"]
)
SETTLEMENT_RELEASES = Counter(
    "wattswap_settlement_releases_total",
    "Pairs released back to the book (e.g. no feasible price), then put on cooldown",
    ["reason"],
)
RELAYER_IN_FLIGHT = Gauge("wattswap_relayer_in_flight", "Submitted, unconfirmed settlements")
LISTENER_LAG = Gauge("wattswap_listener_lag_blocks", "Chain head minus the listener cursor")
LISTENER_REORGS = Counter("wattswap_listener_reorgs_total", "Reorgs the listener rolled back")
FUNDING_CANCELS = Counter(
    "wattswap_funding_cancels_total", "Orders auto-cancelled for lack of funds", ["reason"]
)
INVARIANT_VIOLATIONS = Counter(
    "wattswap_matcher_invariant_violations_total",
    "Matcher produced a pair the exchange rejected as malformed (a bug)",
)
