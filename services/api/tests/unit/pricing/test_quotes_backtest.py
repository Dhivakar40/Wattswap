"""Signed quotes (against the Foundry vectors) and the backtest CLI."""

import csv
import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account

from app.pricing import backtest
from app.pricing.model import CellInput, MarketInputs, demand_terms, supply_terms
from app.pricing.params import PricingParams
from app.pricing.quotes import PriceQuote, recover_quote_signer, sign_quote
from app.pricing.region import cell_of
from app.pricing.trace import CLEAR_SKY_GHI
from app.pricing.weather import parse_location

REPO = Path(__file__).resolve().parents[5]
VECTORS = json.loads((REPO / "packages/shared/vectors/eip712.json").read_text(encoding="utf-8"))
MARKET = "0x3091fb835c35931db16c8b4491333c0472dfb50cef1d2f41dab8f471c8924507"
EXCHANGE = "0x00000000000000000000000000000000000E1001"
KEY = "0x" + "11" * 32


# ------------------------------------------------------------------------------- vectors
QUOTE_VECTORS = [v for v in VECTORS["vectors"] if v["name"].startswith("price_quote")]


def test_the_vector_file_has_the_edge_cases() -> None:
    assert {v["name"] for v in QUOTE_VECTORS} == {
        "price_quote",
        "price_quote_min_price",
        "price_quote_night",
        "price_quote_max_safe_price",
        "price_quote_second_market",
    }


@pytest.mark.parametrize("vector", QUOTE_VECTORS, ids=[v["name"] for v in QUOTE_VECTORS])
def test_python_reproduces_every_foundry_quote_signature_byte_for_byte(
    vector: dict[str, object],
) -> None:
    m = vector["typedData"]["message"]  # type: ignore[index]
    dom = vector["typedData"]["domain"]  # type: ignore[index]
    q = PriceQuote(m["marketId"], m["price"], m["timestamp"])
    signed = sign_quote(VECTORS["testKeys"]["pricer"], dom["chainId"], dom["verifyingContract"], q)
    assert signed.signature == vector["signature"]
    assert signed.signer == vector["signer"]
    assert (
        recover_quote_signer(dom["chainId"], dom["verifyingContract"], q, signed.signature)
        == vector["signer"]
    )


# --------------------------------------------------------------------------- guard rails
@pytest.mark.parametrize(
    "price",
    [150_000.0, Decimal(150_000), True, "150000", None, 0, -1, 2**256],
    ids=["float", "Decimal", "bool", "str", "None", "zero", "negative", "overflow"],
)
def test_a_quote_price_must_be_a_positive_plain_int(price: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        PriceQuote(MARKET, price, 1_800_000_000)  # type: ignore[arg-type]


def test_timestamp_and_market_are_validated_too() -> None:
    with pytest.raises(TypeError):
        PriceQuote(MARKET, 1, 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PriceQuote(MARKET, 1, 0)
    with pytest.raises(ValueError):
        PriceQuote("0x1234", 1, 1)


def test_signatures_are_deterministic_and_bound_to_chain_contract_and_quote() -> None:
    q = PriceQuote(MARKET, 150_000, 1_800_000_000)
    a = sign_quote(KEY, 31337, EXCHANGE, q)
    assert a.signature == sign_quote(KEY, 31337, EXCHANGE, q).signature
    signer = Account.from_key(KEY).address
    assert recover_quote_signer(31337, EXCHANGE, q, a.signature) == signer
    # a different chain, contract, price or timestamp recovers to some OTHER address
    assert recover_quote_signer(1, EXCHANGE, q, a.signature) != signer
    assert recover_quote_signer(31337, "0x" + "22" * 20, q, a.signature) != signer
    assert (
        recover_quote_signer(
            31337, EXCHANGE, PriceQuote(MARKET, 150_001, 1_800_000_000), a.signature
        )
        != signer
    )
    assert (
        recover_quote_signer(
            31337, EXCHANGE, PriceQuote(MARKET, 150_000, 1_800_000_001), a.signature
        )
        != signer
    )
    assert a.to_json()["price"] == 150_000 and isinstance(a.to_json()["price"], int)


# -------------------------------------------------------------------------------- backtest
def synthetic_fixture(path: Path, days: int = 9, constant_ghi: int | None = None) -> Path:
    start = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp()) - 86_400
    n = 24 * days
    ghi = [constant_ghi if constant_ghi is not None else CLEAR_SKY_GHI[i % 24] for i in range(n)]
    payload = {
        "hourly": {
            "time": [start + 3600 * i for i in range(n)],
            "shortwave_radiation": ghi,
            "temperature_2m": [22 + 8 * g / 900 for g in ghi],
            "cloud_cover": [30] * n,
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run_cli(tmp_path: Path, *extra: str, live: bool = False) -> tuple[int, str, Path]:
    """One simulated day. 60 s ticks keep the suite fast; `live=True` uses the real 15 s cadence."""
    fixture = synthetic_fixture(tmp_path / "fixture.json")
    out = tmp_path / "series.csv"
    cadence = [] if live or "--tick-seconds" in extra else ["--tick-seconds", "60"]
    args = [
        "--lat", "0.1", "--lon", "0.1", "--from", "2026-06-01", "--to", "2026-06-01",
        "--capacity-kw", "5", "--fixture", str(fixture), "--out", str(out), "--json", *cadence, *extra,
    ]  # fmt: skip
    return backtest.run(args), "", out


def test_backtest_summary_and_series_at_the_live_cadence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, out = run_cli(tmp_path, live=True)
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["ticks"] == 5760  # one day at the live 15 s cadence
    assert summary["price_min"] >= 20_000 and summary["price_max"] <= 600_000
    assert (
        summary["price_min"] < summary["price_p05"] <= summary["price_p95"] <= summary["price_max"]
    )
    assert summary["max_step_bps"] <= 25  # the limiter held for every one of the ticks
    assert summary["freezes"] == 0 and summary["weather_hours_missing"] == 0
    assert summary["breaker_ticks"] == {"NORMAL": 5760}
    assert float(summary["realized_vol_bps"]) > 0
    assert set(summary["clamp_counts"]) <= {
        "s_min",
        "limiter_up",
        "limiter_down",
        "ratio_cap",
        "raw_floor",
        "raw_ceiling",
    }

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 96  # one row per 15 minutes
    assert list(rows[0]) == list(backtest.FIELDS)
    assert (
        rows[0]["ts_iso"] == "2026-06-01T00:00:00Z" and rows[1]["ts_iso"] == "2026-06-01T00:15:00Z"
    )
    assert rows[0]["price"] == rows[0]["raw_price"]  # the first tick has no history
    assert all(r["price"].isdigit() for r in rows)  # integers only, no float formatting


def test_backtest_first_row_matches_a_hand_calculation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, out = run_cli(tmp_path, "--inventory", "fixed:24000")
    first = next(csv.DictReader(out.open(encoding="utf-8")))
    # cell (0, 0) has its centre at 0.25 deg E: local hour 0.25 / 15 = 1/60 h, multiplier
    # 0.55 + (0.52 - 0.55) / 60 = 0.5495, load = 4.5 * 0.5495 = 2.47275 kW; S_eff = 1.0 kW
    assert first["demand_kw"] == "2.472750" and first["supply_kw"] == "1.000000"
    assert int(first["raw_price"]) == round(150_000 * 2.47275**0.6)


def test_backtest_is_deterministic(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _, _, out_a = run_cli(a)
    _, _, out_b = run_cli(b)
    assert out_a.read_bytes() == out_b.read_bytes()


def test_backtest_text_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixture = synthetic_fixture(tmp_path / "f.json")
    code = backtest.run(
        [
            "--lat",
            "0.1",
            "--lon",
            "0.1",
            "--from",
            "2026-06-01",
            "--to",
            "2026-06-01",
            "--fixture",
            str(fixture),
        ]
    )
    text = capsys.readouterr().out
    assert (
        code == 0 and "price (applied)" in text and "realized vol" in text and "mUSDC/kWh" in text
    )


def test_backtest_honours_parameter_overrides_and_tick_seconds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    params = tmp_path / "p.json"
    params.write_text(json.dumps({"p_base": "160000", "epsilon": "0.5"}), encoding="utf-8")
    code, _, _ = run_cli(tmp_path, "--params-file", str(params))
    assert code == 0
    s = json.loads(capsys.readouterr().out)
    assert s["ticks"] == 1440  # 60 s ticks over one day


def test_backtest_inventory_none_prices_the_night_higher_than_a_stocked_night(
    tmp_path: Path,
) -> None:
    _, _, none = run_cli(tmp_path, "--inventory", "none")
    first_none = next(csv.DictReader(none.open(encoding="utf-8")))
    _, _, stocked = run_cli(tmp_path, "--inventory", "fixed:24000")
    first_stocked = next(csv.DictReader(stocked.open(encoding="utf-8")))
    assert int(first_none["raw_price"]) > int(first_stocked["raw_price"])
    assert (
        int(first_none["raw_price"]) < 600_000
    )  # scarce (about 0.39 mUSDC/kWh), but not the ceiling


@pytest.mark.parametrize(
    "extra",
    [
        ["--to", "2026-05-01"],  # ends before it starts
        ["--inventory", "magic"],
        ["--params-file", "does-not-exist.json"],
    ],
    ids=["dates", "inventory", "params-file"],
)
def test_backtest_bad_input_exits_2(
    tmp_path: Path, extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = synthetic_fixture(tmp_path / "f.json")
    base = [
        "--lat",
        "0.1",
        "--lon",
        "0.1",
        "--from",
        "2026-06-01",
        "--to",
        "2026-06-02",
        "--fixture",
        str(fixture),
    ]
    args = (
        base + extra
        if "--to" not in extra
        else [a for a in base if a not in ("2026-06-02",)][:-2] + extra
    )
    if "--to" in extra:
        args = [
            "--lat",
            "0.1",
            "--lon",
            "0.1",
            "--from",
            "2026-06-01",
            "--fixture",
            str(fixture),
            *extra,
        ]
    assert backtest.run(args) == 2
    assert "error:" in capsys.readouterr().err


def test_backtest_rejects_invalid_parameters_and_missing_weather(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = synthetic_fixture(tmp_path / "f.json")
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"max_step_bps": 60}), encoding="utf-8"
    )  # limiter could reach the breaker
    args = [
        "--lat",
        "0.1",
        "--lon",
        "0.1",
        "--from",
        "2026-06-01",
        "--to",
        "2026-06-02",
        "--fixture",
        str(fixture),
    ]
    assert backtest.run([*args, "--params-file", str(bad)]) == 2
    assert "limiter alone" in capsys.readouterr().err
    # a period the fixture does not cover produces no ticks
    far = [
        "--lat",
        "0.1",
        "--lon",
        "0.1",
        "--from",
        "2027-01-01",
        "--to",
        "2027-01-02",
        "--fixture",
        str(fixture),
    ]
    assert backtest.run(far) == 2
    assert "no ticks" in capsys.readouterr().err
    notjson = tmp_path / "x.json"
    notjson.write_text("nope", encoding="utf-8")
    assert backtest.run([*args[:-1], str(notjson)]) == 2


def test_backtest_replay_matches_the_pure_engine(tmp_path: Path) -> None:
    """The replay is the production engine, not a re-implementation."""
    fixture = synthetic_fixture(tmp_path / "f.json")
    series = parse_location(json.loads(fixture.read_text(encoding="utf-8")))
    cell = cell_of(100_000, 100_000)
    p = PricingParams()
    start = int(datetime(2026, 6, 1, 12, tzinfo=UTC).timestamp())
    scn = backtest.Scenario(cell, 5000, start, start + 900, p, inventory_model="fixed:24000")
    rows, ticks, _ = backtest.replay(scn, series)
    point = series[start]
    i = MarketInputs(
        start,
        (
            CellInput(
                cell.id,
                5000,
                1,
                cell.center_lon_e6,
                point.ghi_wm2,
                point.temp_c,
                point.cloud_cover_pct,
                0,
                "archive",
            ),
        ),
        inventory_wh=24_000,
        inventory_source="x",
    )
    s, d = supply_terms(i, p), demand_terms(i, p)
    assert ticks[0].drivers["supply"]["effective_kw"] == f"{s.effective_kw:.6f}"
    assert ticks[0].drivers["demand"]["demand_kw"] == f"{d.demand_kw:.6f}"
    assert math.isclose(float(rows[0]["demand_kw"]), float(d.demand_kw), abs_tol=1e-6)
