"""Open-Meteo is mocked with respx: normal data, timeouts, 429s, 5xx, malformed bodies, rate budget."""

import json
import random
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.pricing.region import Cell, cell_of
from app.pricing.weather import (
    OpenMeteo,
    Series,
    WeatherError,
    WeatherService,
    parse_batch,
    parse_location,
    parse_retry_after,
)

D = Decimal
FORECAST = "https://api.open-meteo.test/v1/forecast"
ARCHIVE = "https://archive.open-meteo.test/v1/archive"
H0 = 1_800_000_000 - 1_800_000_000 % 86_400  # a UTC midnight
CHENNAI = cell_of(13_082_700, 80_270_700)  # (26, 160)
SYDNEY = cell_of(-33_868_800, 151_209_300)  # (-68, 302)


def location(
    hours: int = 72,
    ghi: Any = None,
    temp: Any = None,
    start: int = H0 - 86_400,
    **extra: Any,
) -> dict[str, Any]:
    """An Open-Meteo-shaped location: 72 hourly values by default (yesterday through tomorrow)."""
    times = [start + 3600 * i for i in range(hours)]
    hourly: dict[str, Any] = {
        "time": times,
        "shortwave_radiation": ghi if ghi is not None else [100 * (i % 10) for i in range(hours)],
        "temperature_2m": temp if temp is not None else [25.0] * hours,
        "cloud_cover": [40] * hours,
        "direct_radiation": [50] * hours,
    }
    hourly.update(extra)
    return {"latitude": 13.25, "longitude": 80.25, "hourly": hourly}


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(
    clock: Clock | None = None, retries: int = 2
) -> tuple[WeatherService, OpenMeteo, httpx.AsyncClient, Clock]:
    clock = clock or Clock()
    client = httpx.AsyncClient()
    provider = OpenMeteo(
        client,
        forecast_url=FORECAST,
        archive_url=ARCHIVE,
        retries=retries,
        retry_delay_s=0,
        today=lambda: date(2026, 10, 18),
    )
    svc = WeatherService(provider, clock=clock, rng=random.Random(1), jitter=0.0)
    return svc, provider, client, clock


# ------------------------------------------------------------------------------ parsing
def test_parse_location_accepts_valid_data_as_decimals() -> None:
    s = parse_location(location(hours=3, ghi=[0, 250.5, 800], temp=[20, 21.5, 30]))
    assert list(s) == [H0 - 86_400 + 3600 * i for i in range(3)]
    p = s[H0 - 86_400 + 3600]
    assert p.ghi_wm2 == D("250.5") and p.temp_c == D("21.5") and p.cloud_cover_pct == 40
    assert isinstance(p.ghi_wm2, Decimal)  # never a float past the edge


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "nope",
        [],
        {},
        {"hourly": []},
        {"hourly": {}},
        {"hourly": {"time": [], "shortwave_radiation": [], "temperature_2m": []}},
        {"hourly": {"time": [3600], "shortwave_radiation": [1, 2], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": "x", "temperature_2m": [1]}},
        {
            "hourly": {
                "time": ["2026-10-18T00:00"],
                "shortwave_radiation": [1],
                "temperature_2m": [1],
            }
        },
        {"hourly": {"time": [3601], "shortwave_radiation": [1], "temperature_2m": [1]}},
        {"hourly": {"time": [True], "shortwave_radiation": [1], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": [None], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": [99999], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": [-5], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": [float("nan")], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": [float("inf")], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": ["5"], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": [True], "temperature_2m": [1]}},
        {"hourly": {"time": [3600], "shortwave_radiation": [1], "temperature_2m": [500]}},
    ],
)
def test_malformed_locations_are_rejected(payload: Any) -> None:
    with pytest.raises(WeatherError) as exc:
        parse_location(payload)
    assert exc.value.kind == "malformed"


def test_bad_hours_are_skipped_but_good_hours_survive() -> None:
    s = parse_location(location(hours=4, ghi=[100, None, 99999, 300], temp=[20, 20, 20, 20]))
    assert sorted(s) == [H0 - 86_400, H0 - 86_400 + 3 * 3600]


def test_batch_must_match_the_requested_cells() -> None:
    with pytest.raises(WeatherError):
        parse_batch([location(hours=2)], [CHENNAI, SYDNEY])  # one location for two cells
    ok, bad = parse_batch([location(hours=2), {"hourly": {}}], [CHENNAI, SYDNEY])
    assert list(ok) == [CHENNAI.id] and SYDNEY.id in bad
    with pytest.raises(WeatherError):
        parse_batch([{}, {}], [CHENNAI, SYDNEY])  # nothing usable at all


def test_retry_after_parsing() -> None:
    assert parse_retry_after("120") == 120
    assert parse_retry_after("0") == 0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None and parse_retry_after("-5") is None
    assert parse_retry_after("999999") is None  # absurd values are ignored


# --------------------------------------------------------------------- normal operation
@respx.mock
async def test_one_batched_request_covers_every_cell() -> None:
    route = respx.get(FORECAST).mock(
        return_value=httpx.Response(200, json=[location(), location(), location()])
    )
    svc, _, client, _ = make()
    cells = [CHENNAI, SYDNEY, Cell(10, 10)]
    r = await svc.refresh(cells)
    assert r.status == "ok" and r.cells_updated == 3
    assert route.call_count == 1  # three cells, ONE request
    q = route.calls.last.request.url.params
    assert q["latitude"] == "13.2500,-33.7500,5.2500"
    assert q["longitude"] == "80.2500,151.2500,5.2500"
    assert q["hourly"] == "shortwave_radiation,direct_radiation,cloud_cover,temperature_2m"
    assert (q["timezone"], q["timeformat"], q["past_days"], q["forecast_days"]) == (
        "GMT",
        "unixtime",
        "1",
        "2",
    )
    await client.aclose()


@respx.mock
async def test_single_location_response_is_an_object_not_a_list() -> None:
    respx.get(FORECAST).mock(return_value=httpx.Response(200, json=location()))
    svc, _, client, _ = make()
    assert (await svc.refresh([CHENNAI])).status == "ok"
    await client.aclose()


@respx.mock
async def test_lookup_interpolates_and_reports_age() -> None:
    respx.get(FORECAST).mock(
        return_value=httpx.Response(
            200, json=location(hours=3, ghi=[100, 300, 500], temp=[20, 30, 40])
        )
    )
    svc, _, client, clock = make()
    await svc.refresh([CHENNAI])
    h = H0 - 86_400
    hit = svc.lookup(CHENNAI.id, h + 1800)  # halfway between 100 and 300 W/m2, 20 and 30 C
    assert hit.point is not None and hit.point.ghi_wm2 == 200 and hit.point.temp_c == 25
    assert hit.age_s == 0 and hit.source == "live"
    clock.t += 1860
    assert svc.lookup(CHENNAI.id, h).age_s == 1860  # 31 minutes: stale for the breaker
    assert svc.lookup(CHENNAI.id, h + 3 * 3600).point is None  # outside the data -> no weather
    assert svc.lookup("99:99", h).source == "none"
    await client.aclose()


# --------------------------------------------------------------------------- failures
@respx.mock
async def test_timeout_is_retried_then_reported_and_last_good_data_is_kept() -> None:
    route = respx.get(FORECAST).mock(return_value=httpx.Response(200, json=location()))
    svc, _, client, clock = make()
    await svc.refresh([CHENNAI])
    h = H0 - 86_400 + 5 * 3600

    route.mock(side_effect=httpx.ReadTimeout("slow"))
    clock.t += 600
    r = await svc.refresh([CHENNAI])
    assert r.status == "failed" and r.error is not None and r.error.startswith("timeout")
    assert route.call_count == 1 + 3  # the failed poll made 1 + 2 retries
    kept = svc.lookup(CHENNAI.id, h)
    assert kept.point is not None and kept.age_s == 600 and kept.source == "last_good"
    await client.aclose()


@respx.mock
async def test_connection_error_is_reported() -> None:
    respx.get(FORECAST).mock(side_effect=httpx.ConnectError("refused"))
    svc, _, client, _ = make()
    r = await svc.refresh([CHENNAI])
    assert r.status == "failed" and r.error is not None and r.error.startswith("connection")
    assert svc.lookup(CHENNAI.id, H0).source == "none"  # nothing was ever fetched
    await client.aclose()


@respx.mock
async def test_429_honours_retry_after_and_is_never_retried_in_the_same_poll() -> None:
    route = respx.get(FORECAST).mock(
        return_value=httpx.Response(429, headers={"retry-after": "300"})
    )
    svc, _, client, clock = make()
    r = await svc.refresh([CHENNAI])
    assert r.status == "failed" and route.call_count == 1  # no hammering
    assert r.next_allowed_in_s >= 300 and "http_429" in str(r.error)

    clock.t += 100  # still inside the retry-after: no request at all
    assert (await svc.refresh([CHENNAI])).status == "skipped" and route.call_count == 1
    clock.t += 250  # past it
    route.mock(return_value=httpx.Response(200, json=location()))
    assert (await svc.refresh([CHENNAI])).status == "ok" and route.call_count == 2
    await client.aclose()


@respx.mock
async def test_429_without_retry_after_backs_off_exponentially() -> None:
    route = respx.get(FORECAST).mock(return_value=httpx.Response(429))
    svc, _, client, clock = make()
    delays = []
    for _ in range(4):
        r = await svc.refresh([CHENNAI])
        delays.append(r.next_allowed_in_s)
        clock.t += r.next_allowed_in_s + 1
    assert delays == [30, 60, 120, 240]  # base 30 s doubling, jitter disabled in this test
    assert route.call_count == 4
    await client.aclose()


@respx.mock
async def test_5xx_is_retried_then_reported() -> None:
    route = respx.get(FORECAST).mock(return_value=httpx.Response(503))
    svc, _, client, _ = make()
    r = await svc.refresh([CHENNAI])
    assert r.status == "failed" and "http_5xx" in str(r.error) and route.call_count == 3
    await client.aclose()


@respx.mock
async def test_transient_5xx_recovers_within_a_poll() -> None:
    route = respx.get(FORECAST).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=location())]
    )
    svc, _, client, _ = make()
    assert (await svc.refresh([CHENNAI])).status == "ok" and route.call_count == 2
    await client.aclose()


@respx.mock
async def test_4xx_is_not_retried() -> None:
    route = respx.get(FORECAST).mock(return_value=httpx.Response(400, json={"reason": "bad"}))
    svc, _, client, _ = make()
    r = await svc.refresh([CHENNAI])
    assert r.status == "failed" and "http_4xx" in str(r.error) and route.call_count == 1
    await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>gateway</html>"),
        httpx.Response(200, content=b""),
        httpx.Response(200, json={"error": True, "reason": "limit"}),
        httpx.Response(200, json=[]),
        httpx.Response(
            200,
            json={"hourly": {"time": [3600], "shortwave_radiation": [1, 2], "temperature_2m": [1]}},
        ),
        httpx.Response(
            200,
            content=b'{"hourly":{"time":[3600],"shortwave_radiation":[NaN],"temperature_2m":[1]}}',
        ),
        httpx.Response(200, json=[location(), location()]),  # two locations for one cell
    ],
    ids=["html", "empty", "error-object", "empty-list", "length-mismatch", "nan", "wrong-count"],
)
@respx.mock
async def test_malformed_responses_count_as_failures(response: httpx.Response) -> None:
    respx.get(FORECAST).mock(return_value=response)
    svc, _, client, _ = make()
    r = await svc.refresh([CHENNAI])
    assert r.status == "failed" and "malformed" in str(r.error)
    assert svc.lookup(CHENNAI.id, H0).point is None
    await client.aclose()


@respx.mock
async def test_malformed_response_after_good_data_keeps_the_good_data() -> None:
    route = respx.get(FORECAST).mock(return_value=httpx.Response(200, json=location()))
    svc, _, client, clock = make()
    await svc.refresh([CHENNAI])
    route.mock(return_value=httpx.Response(200, json={"hourly": {}}))
    clock.t += 700
    assert (await svc.refresh([CHENNAI])).status == "failed"
    kept = svc.lookup(CHENNAI.id, H0 - 86_400 + 3600)
    assert kept.point is not None and kept.age_s == 700
    await client.aclose()


@respx.mock
async def test_partial_response_updates_only_the_good_cells() -> None:
    respx.get(FORECAST).mock(
        return_value=httpx.Response(200, json=[location(), {"hourly": {"time": []}}])
    )
    svc, _, client, _ = make()
    r = await svc.refresh([CHENNAI, SYDNEY])
    assert r.status == "partial" and r.cells_updated == 1
    assert svc.lookup(CHENNAI.id, H0 - 86_400 + 3600).point is not None
    assert svc.lookup(SYDNEY.id, H0).source == "none"
    await client.aclose()


@respx.mock
async def test_recovery_resets_the_backoff() -> None:
    route = respx.get(FORECAST).mock(return_value=httpx.Response(429))
    svc, _, client, clock = make()
    r1 = await svc.refresh([CHENNAI])
    clock.t += r1.next_allowed_in_s + 1
    route.mock(return_value=httpx.Response(200, json=location()))
    assert (await svc.refresh([CHENNAI])).status == "ok"
    route.mock(return_value=httpx.Response(429))
    assert (await svc.refresh([CHENNAI])).next_allowed_in_s == 30  # back to the base delay
    await client.aclose()


# ---------------------------------------------------------------------- rate budget
@respx.mock
async def test_request_volume_is_independent_of_the_number_of_cells_and_bounded_when_failing() -> (
    None
):
    route = respx.get(FORECAST).mock(
        return_value=httpx.Response(200, json=[location() for _ in range(60)])
    )
    svc, _, client, clock = make()
    cells = [Cell(i, 0) for i in range(60)]
    for _ in range(6):  # six polls (an hour at 10 minutes)
        await svc.refresh(cells)
        clock.t += 600
    assert route.call_count == 6  # 6 requests/hour for 60 cells; Open-Meteo allows 5 000/hour

    route.mock(return_value=httpx.Response(500))
    before = route.call_count
    for _ in range(120):  # a failing hour polled every 30 s must not turn into a request storm
        await svc.refresh(cells)
        clock.t += 30
    attempts = (route.call_count - before) / 3  # 3 HTTP calls per failed poll
    assert attempts <= 8  # exponential backoff caps it
    await client.aclose()


# ------------------------------------------------------------------------ history days
@respx.mock
async def test_history_uses_the_archive_for_old_days_and_caches_forever() -> None:
    archive = respx.get(ARCHIVE).mock(
        return_value=httpx.Response(
            200, json=location(hours=24, start=1_790_000_000 - 1_790_000_000 % 86_400)
        )
    )
    forecast = respx.get(FORECAST).mock(return_value=httpx.Response(200, json=location()))
    svc, _, client, _ = make()
    ts = 1_790_000_000 - 1_790_000_000 % 86_400 + 6 * 3600  # 2026-09-... > 6 days before "today"
    look = await svc.lookup_history(CHENNAI, ts)
    assert look.point is not None and look.source == "archive" and look.age_s == 0
    again = await svc.lookup_history(CHENNAI, ts + 3600)
    assert again.point is not None
    assert archive.call_count == 1 and forecast.call_count == 0  # second hour came from the cache
    q = archive.calls.last.request.url.params
    assert q["start_date"] == q["end_date"] and q["timezone"] == "GMT"
    await client.aclose()


@respx.mock
async def test_recent_history_uses_the_forecast_endpoint_with_dates() -> None:
    forecast = respx.get(FORECAST).mock(
        return_value=httpx.Response(200, json=location(hours=24, start=H0 - 2 * 86_400))
    )
    svc, _, client, _ = make()
    look = await svc.lookup_history(CHENNAI, H0 - 2 * 86_400 + 3600)
    assert look.point is not None and forecast.call_count == 1
    assert "start_date" in forecast.calls.last.request.url.params
    await client.aclose()


@respx.mock
async def test_history_failure_reports_no_data_and_retries_next_time() -> None:
    archive = respx.get(ARCHIVE).mock(side_effect=httpx.ConnectError("down"))
    svc, _, client, _ = make(retries=0)
    ts = 1_700_000_000
    assert (await svc.lookup_history(CHENNAI, ts)).source == "none"
    archive.mock(return_value=httpx.Response(200, json=location(hours=24, start=ts - ts % 86_400)))
    assert (await svc.lookup_history(CHENNAI, ts)).source == "archive"  # failures are not cached
    await client.aclose()


def test_series_type_is_hourly_keyed_by_unix_time() -> None:
    s: Series = parse_location(location(hours=2))
    assert all(k % 3600 == 0 for k in s)
    assert json.dumps(sorted(s)) is not None
