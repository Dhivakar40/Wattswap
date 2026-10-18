"""meter-sim command line: keygen, provision (dev only) and run."""

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account

from meter_sim.client import IngestClient
from meter_sim.device import MeterDevice, MeterSpec, SimulatedMeter
from meter_sim.model import GenerationModel, GenerationParams
from meter_sim.provision import register_meter, save_meters_config, siwe_login
from meter_sim.runner import RunConfig, run
from meter_sim.weather import NoWeather, OpenMeteoProvider, WeatherProvider

BANNER = "WattSwap meter SIMULATOR: readings are simulated, not from real hardware."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meter-sim",
        description="WattSwap smart-meter SIMULATOR (labeled simulation, not real hardware).",
    )
    sub = parser.add_subparsers(dest="command")

    keygen = sub.add_parser("keygen", help="generate a device key and print its address")
    keygen.add_argument("--out", type=Path, required=True, help="file to write the key to")

    prov = sub.add_parser(
        "provision",
        help="DEV ONLY: sign in as an owner wallet, register a meter, append it to meters.json",
    )
    prov.add_argument("--api", default=os.environ.get("METER_SIM_API_URL", "http://localhost:8000"))
    prov.add_argument("--owner-key-env", default="OWNER_PRIVATE_KEY")
    prov.add_argument("--device-key", type=Path, required=True)
    prov.add_argument("--config", type=Path, default=Path("meters.json"))
    prov.add_argument("--label", default="simulated-rooftop")
    prov.add_argument("--lat", type=float, default=13.0827)
    prov.add_argument("--lon", type=float, default=80.2707)
    prov.add_argument("--capacity-w", type=int, default=5000)
    prov.add_argument("--siwe-domain", default=os.environ.get("SIWE_DOMAIN", "localhost:3000"))

    runp = sub.add_parser("run", help="emit signed readings to the API")
    runp.add_argument("--api", default=os.environ.get("METER_SIM_API_URL", "http://localhost:8000"))
    runp.add_argument("--config", type=Path, default=Path("meters.json"))
    runp.add_argument(
        "--interval-seconds",
        type=int,
        default=int(os.environ.get("READING_INTERVAL_SECONDS", "900")),
    )
    runp.add_argument(
        "--demo",
        action="store_true",
        help="ACCELERATED demo: emit one simulated interval every --demo-seconds real seconds",
    )
    runp.add_argument("--demo-seconds", type=float, default=20.0)
    runp.add_argument(
        "--backfill-hours",
        type=float,
        default=24.0,
        help="in --demo mode, start this many simulated hours in the past",
    )
    runp.add_argument("--no-weather", action="store_true", help="skip Open-Meteo (clear-sky model)")
    runp.add_argument("--seed", type=int, default=None, help="seed the noise (reproducible runs)")
    runp.add_argument("--noise", type=float, default=0.03)
    runp.add_argument("--max-intervals", type=int, default=None)
    return parser


def cmd_keygen(out: Path) -> int:
    if out.exists():
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    account = Account.create()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("0x" + account.key.hex() + "\n", encoding="utf-8")
    try:
        out.chmod(0o600)
    except OSError:  # not supported on every filesystem (e.g. Windows)
        pass
    print(f"device address: {account.address}")
    print(f"key written to: {out}  (keep it secret; never commit it)")
    return 0


async def cmd_provision(args: argparse.Namespace) -> int:
    owner_key = os.environ.get(args.owner_key_env)
    if not owner_key:
        print(f"set {args.owner_key_env} to the OWNER wallet key (dev only)", file=sys.stderr)
        return 1
    device_key = args.device_key.read_text(encoding="utf-8").strip()
    device_address = Account.from_key(device_key).address
    async with httpx.AsyncClient(base_url=args.api, timeout=60) as http:
        cfg = (await http.get("/api/v1/config")).json()
        session = await siwe_login(
            http, owner_key, domain=args.siwe_domain, chain_id=int(cfg["chain_id"])
        )
        meter = await register_meter(
            session,
            label=args.label,
            device_address=device_address,
            capacity_w=args.capacity_w,
            lat=args.lat,
            lon=args.lon,
        )
    save_meters_config(
        args.config,
        {
            "meter_id": meter["meter_id"],
            "device_key_file": str(args.device_key),
            "lat": args.lat,
            "lon": args.lon,
            "capacity_w": args.capacity_w,
            "label": args.label,
        },
    )
    print(f"registered meter {meter['meter_id']} ({meter['register_status']})")
    print(f"added to {args.config}")
    return 0


def load_specs(path: Path) -> list[MeterSpec]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    specs: list[MeterSpec] = []
    for m in data["meters"]:
        key_file = Path(m["device_key_file"])
        key_path = key_file if key_file.is_absolute() else base / key_file
        if not key_path.exists():
            key_path = Path(m["device_key_file"])
        specs.append(
            MeterSpec(
                meter_id=m["meter_id"],
                private_key=key_path.read_text(encoding="utf-8").strip(),
                lat=float(m["lat"]),
                lon=float(m["lon"]),
                capacity_w=int(m["capacity_w"]),
                label=m.get("label", "simulated-meter"),
            )
        )
    return specs


async def cmd_run(args: argparse.Namespace) -> int:
    specs = load_specs(args.config)
    if not specs:
        print(f"no meters in {args.config}; run `meter-sim provision` first", file=sys.stderr)
        return 1
    logging.getLogger("meter_sim").warning(BANNER)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops do not support signal handlers
            pass

    weather: WeatherProvider = NoWeather() if args.no_weather else OpenMeteoProvider()
    rng = random.Random(args.seed)  # noqa: S311 - simulation noise, not security
    model = GenerationModel(weather, GenerationParams(noise_sigma=args.noise), rng)
    async with httpx.AsyncClient(base_url=args.api, timeout=30) as http:
        ingest = IngestClient(http)
        cfg = await ingest.config()
        devices: list[MeterDevice] = [
            SimulatedMeter(s, model, int(cfg["chain_id"]), str(cfg["energy_token"])) for s in specs
        ]
        start_ts = None
        if args.demo:
            start_ts = int(time.time() - args.backfill_hours * 3600)
        stats = await run(
            devices,
            ingest,
            RunConfig(
                interval_seconds=args.interval_seconds,
                start_ts=start_ts,
                demo=args.demo,
                demo_seconds_per_interval=args.demo_seconds,
                max_intervals=args.max_intervals,
            ),
            stop,
        )
    if isinstance(weather, OpenMeteoProvider):
        await weather.aclose()
    print(
        f"done: posted={stats.posted} duplicates={stats.duplicates} "
        f"no_energy={stats.skipped_no_energy} rejected={stats.rejected}"
    )
    return 1 if stats.rejected else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "keygen":
        return cmd_keygen(args.out)
    if args.command == "provision":
        return asyncio.run(cmd_provision(args))
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
