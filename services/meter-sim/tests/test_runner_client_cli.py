import asyncio
import random
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from meter_sim.cli import build_parser, cmd_keygen, load_specs, main
from meter_sim.client import IngestClient, IngestRejectedError, IngestResult
from meter_sim.device import Measurement, MeterSpec, SimulatedMeter
from meter_sim.model import Generation, GenerationModel, GenerationParams
from meter_sim.provision import build_siwe_message, save_meters_config
from meter_sim.runner import RunConfig, align_down, run
from meter_sim.signing import SignedReading, sign_reading
from meter_sim.weather import WeatherPoint

METER = "0x" + "ab" * 32
KEY = "0x" + "11" * 32
TOKEN = "0x00000000000000000000000000000000000E1002"


def reading(start: int = 1_800_000_000, wh: int = 100) -> SignedReading:
    return sign_reading(KEY, 31337, TOKEN, METER, wh, start, 900)


# --------------------------------------------------------------------------- device
class ConstantWeather:
    def __init__(self, ghi: float) -> None:
        self.points = [WeatherPoint(ghi, 25.0)] * 24

    async def day(self, lat: float, lon: float, day: date) -> list[WeatherPoint] | None:
        return self.points


async def test_simulated_meter_skips_zero_energy_and_signs_otherwise() -> None:
    spec = MeterSpec(METER, KEY, 13.0, 80.0, 5000)
    dark = SimulatedMeter(
        spec,
        GenerationModel(ConstantWeather(0.0), GenerationParams(noise_sigma=0), random.Random(1)),
        31337,
        TOKEN,
    )
    assert await dark.read_interval(1_800_000_000, 900) is None
    sunny = SimulatedMeter(
        spec,
        GenerationModel(ConstantWeather(800.0), GenerationParams(noise_sigma=0), random.Random(1)),
        31337,
        TOKEN,
    )
    m = await sunny.read_interval(1_800_000_000, 900)
    assert m is not None and m.reading.amount_wh > 0 and m.reading.signature.startswith("0x")
    assert m.reading.meter_id == METER


# --------------------------------------------------------------------------- client
@respx.mock
async def test_client_accepts_new_and_duplicate() -> None:
    body = {"reading": {"mint": {"status": "pending"}}, "duplicate": False}
    route = respx.post("http://api/api/v1/readings").mock(
        side_effect=[
            httpx.Response(202, json=body),
            httpx.Response(200, json={**body, "duplicate": True}),
        ]
    )
    async with httpx.AsyncClient(base_url="http://api") as http:
        c = IngestClient(http)
        assert await c.post(reading()) == IngestResult(False, "pending")
        assert (await c.post(reading())).duplicate is True
    sent = route.calls.last.request.read()
    assert b'"simulated":true' in sent.replace(b" ", b"")


@respx.mock
async def test_client_retries_429_and_5xx_then_succeeds() -> None:
    ok = {"reading": {"mint": None}, "duplicate": False}
    respx.post("http://api/api/v1/readings").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(503),
            httpx.Response(202, json=ok),
        ]
    )
    async with httpx.AsyncClient(base_url="http://api") as http:
        result = await IngestClient(http, backoff_s=0).post(reading())
    assert result == IngestResult(False, None)


@respx.mock
async def test_client_does_not_retry_permanent_rejections() -> None:
    route = respx.post("http://api/api/v1/readings").mock(
        return_value=httpx.Response(
            409, json={"error": {"code": "interval_out_of_order", "message": "backfill in order"}}
        )
    )
    async with httpx.AsyncClient(base_url="http://api") as http:
        with pytest.raises(IngestRejectedError) as exc:
            await IngestClient(http, backoff_s=0).post(reading())
    assert exc.value.code == "interval_out_of_order"
    assert route.call_count == 1


@respx.mock
async def test_client_gives_up_after_retries_on_transport_error() -> None:
    respx.post("http://api/api/v1/readings").mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient(base_url="http://api") as http:
        with pytest.raises(httpx.ConnectError):
            await IngestClient(http, retries=1, backoff_s=0).post(reading())


# --------------------------------------------------------------------------- runner
class FakeDevice:
    meter_id = METER

    def __init__(self, none_for: set[int] | None = None) -> None:
        self.calls: list[int] = []
        self.none_for = none_for or set()

    async def read_interval(self, start_ts: int, seconds: int) -> Measurement | None:
        self.calls.append(start_ts)
        if start_ts in self.none_for:
            return None
        return Measurement(reading(start_ts), Generation(100, "clear-sky-fallback", 1500))


class FakeClient(IngestClient):
    def __init__(self, reject_at: int | None = None) -> None:
        self.sent: list[int] = []
        self.reject_at = reject_at

    async def post(self, r: SignedReading) -> IngestResult:
        if r.interval_start == self.reject_at:
            raise IngestRejectedError(409, "interval_out_of_order", "no")
        self.sent.append(r.interval_start)
        return IngestResult(False, "pending")


async def test_runner_backfills_in_order_without_sleeping_in_realtime_mode() -> None:
    dev, client = FakeDevice(), FakeClient()
    start = 1_800_000_000 - 1_800_000_000 % 900
    stats = await run(
        [dev],
        client,
        RunConfig(start_ts=start, max_intervals=4),
        asyncio.Event(),
        now=lambda: start + 10 * 900,
    )
    assert client.sent == [start + i * 900 for i in range(4)]
    assert stats.posted == 4 and stats.intervals == 4


async def test_runner_demo_mode_paces_and_skips_dark_intervals() -> None:
    start = 1_800_000_000 - 1_800_000_000 % 900
    dev = FakeDevice(none_for={start + 900})
    client = FakeClient()
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    stats = await run(
        [dev],
        client,
        RunConfig(start_ts=start, demo=True, demo_seconds_per_interval=20.0, max_intervals=3),
        asyncio.Event(),
        now=lambda: start + 10 * 900,
        sleep=fake_sleep,
    )
    assert sleeps == [20.0, 20.0]  # the dark interval emitted nothing, so it is not paced
    assert stats.skipped_no_energy == 1 and stats.posted == 2


async def test_runner_records_rejections_and_continues() -> None:
    start = 1_800_000_000 - 1_800_000_000 % 900
    client = FakeClient(reject_at=start + 900)
    stats = await run(
        [FakeDevice()],
        client,
        RunConfig(start_ts=start, max_intervals=3),
        asyncio.Event(),
        now=lambda: start + 10 * 900,
    )
    assert stats.rejected == 1 and stats.posted == 2


async def test_runner_never_emits_future_intervals() -> None:
    start = 1_800_000_000 - 1_800_000_000 % 900
    dev = FakeDevice()
    stop = asyncio.Event()
    # "now" is mid-way through the first interval: nothing may be emitted until it ends
    task = asyncio.create_task(
        run([dev], FakeClient(), RunConfig(start_ts=start), stop, now=lambda: start + 100)
    )
    await asyncio.sleep(0.05)
    stop.set()
    stats = await task
    assert dev.calls == [] and stats.posted == 0


def test_align_down() -> None:
    assert align_down(1_800_000_123, 900) == 1_800_000_123 - 1_800_000_123 % 900


# --------------------------------------------------------------------------- cli / config
def test_help_mentions_simulator(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "SIMULATOR" in capsys.readouterr().out


def test_parser_prog_and_demo_flag() -> None:
    parser = build_parser()
    assert parser.prog == "meter-sim"
    args = parser.parse_args(["run", "--demo", "--demo-seconds", "20"])
    assert args.demo is True and args.demo_seconds == 20.0


def test_keygen_writes_key_and_refuses_to_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "keys" / "m1.key"
    assert cmd_keygen(out) == 0
    assert out.read_text().startswith("0x") and "device address: 0x" in capsys.readouterr().out
    assert cmd_keygen(out) == 1


def test_meters_config_roundtrip(tmp_path: Path) -> None:
    key = tmp_path / "m1.key"
    key.write_text(KEY + "\n")
    cfg = tmp_path / "meters.json"
    entry = {
        "meter_id": METER,
        "device_key_file": "m1.key",
        "lat": 13.0,
        "lon": 80.0,
        "capacity_w": 5000,
        "label": "x",
    }
    save_meters_config(cfg, entry)
    save_meters_config(cfg, {**entry, "capacity_w": 6000})  # same meter: replaced, not duplicated
    specs = load_specs(cfg)
    assert len(specs) == 1 and specs[0].capacity_w == 6000 and specs[0].private_key == KEY


def test_siwe_message_format() -> None:
    msg = build_siwe_message(
        domain="localhost:3000",
        address="0x7555a3905b1fbcd637b385b7063a356bbaed4f09",
        uri="http://localhost:3000",
        chain_id=31337,
        nonce="abcdef0123456789",
        issued_at=datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC),
    )
    lines = msg.split("\n")
    assert lines[0] == "localhost:3000 wants you to sign in with your Ethereum account:"
    assert lines[1] == "0x7555A3905b1FbCd637b385B7063a356bbAEd4F09"  # EIP-55 checksum
    assert "Chain ID: 31337" in lines and "Issued At: 2026-10-18T12:00:00.000Z" in lines
    assert lines[-1] == "Expiration Time: 2026-10-18T12:05:00.000Z"
