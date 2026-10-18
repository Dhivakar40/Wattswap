"""Interval scheduler: real-time mode and the accelerated DEMO mode."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from meter_sim.client import IngestClient, IngestRejectedError
from meter_sim.device import MeterDevice

log = logging.getLogger("meter_sim.runner")


@dataclass
class RunConfig:
    interval_seconds: int = 900
    # Where simulated time starts, as a unix timestamp. None = start at the current interval.
    start_ts: int | None = None
    # DEMO mode: simulated time runs ahead of real time. One simulated interval is emitted
    # every `demo_seconds_per_interval` real seconds until simulated time reaches the present.
    demo: bool = False
    demo_seconds_per_interval: float = 20.0
    # Stop after this many intervals have been *evaluated* (None = run forever).
    max_intervals: int | None = None


@dataclass
class RunStats:
    intervals: int = 0
    posted: int = 0
    duplicates: int = 0
    skipped_no_energy: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


def align_down(ts: int, interval: int) -> int:
    return ts - ts % interval


async def run(
    devices: list[MeterDevice],
    client: IngestClient,
    cfg: RunConfig,
    stop: asyncio.Event,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RunStats:
    """Emit one signed reading per device per completed interval, strictly in order."""
    stats = RunStats()
    interval = cfg.interval_seconds
    cursor = align_down(int(cfg.start_ts if cfg.start_ts is not None else now()), interval)
    if cfg.start_ts is None:
        cursor -= interval  # begin with the most recently completed interval

    if cfg.demo:
        log.warning(
            "ACCELERATED DEMO MODE: simulated time runs ahead of real time "
            "(1 simulated %ss interval every %ss real). Readings are SIMULATED.",
            interval,
            cfg.demo_seconds_per_interval,
        )
    caught_up_logged = False

    while not stop.is_set():
        if cfg.max_intervals is not None and stats.intervals >= cfg.max_intervals:
            break
        end = cursor + interval
        wait = end - now()
        if wait > 0:
            # Simulated time has reached the present: continue at real-time pace.
            if cfg.demo and not caught_up_logged:
                log.warning("demo mode caught up with real time; continuing at real-time pace")
                caught_up_logged = True
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait + 1.0)
            except TimeoutError:
                pass
            continue

        emitted = False
        for device in devices:
            try:
                measurement = await device.read_interval(cursor, interval)
                if measurement is None:
                    stats.skipped_no_energy += 1
                    continue
                result = await client.post(measurement.reading)
                emitted = True
                if result.duplicate:
                    stats.duplicates += 1
                else:
                    stats.posted += 1
                log.info(
                    "reading sent",
                    extra={
                        "meter_id": device.meter_id,
                        "interval_start": cursor,
                        "wh": measurement.reading.amount_wh,
                        "source": measurement.generation.source,
                        "duplicate": result.duplicate,
                        "accelerated": cfg.demo,
                        "simulated": True,
                    },
                )
            except IngestRejectedError as exc:
                stats.rejected += 1
                stats.errors.append(str(exc))
                log.error("reading rejected: %s", exc, extra={"meter_id": device.meter_id})
        stats.intervals += 1
        cursor = end
        if cfg.demo and emitted and cfg.demo_seconds_per_interval > 0:
            await sleep(cfg.demo_seconds_per_interval)
    return stats
