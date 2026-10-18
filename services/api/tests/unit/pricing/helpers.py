"""Builders for pricing tests."""

from decimal import Decimal

from app.pricing.model import CellInput, MarketInputs

D = Decimal
MIDNIGHT = 1_800_000_000 - 1_800_000_000 % 86_400  # a UTC midnight


def cell(
    capacity_w: int = 5000,
    ghi: str = "0",
    temp: str = "30",
    *,
    age_s: int = 0,
    source: str = "live",
    cloud: str | None = None,
    cell_id: str = "26:0",
    lon_e6: int = 0,
    meters: int = 1,
) -> CellInput:
    return CellInput(
        cell_id=cell_id,
        capacity_w=capacity_w,
        meters=meters,
        center_lon_e6=lon_e6,
        ghi_wm2=D(ghi),
        temp_c=D(temp),
        cloud_cover_pct=None if cloud is None else D(cloud),
        weather_age_s=age_s,
        weather_source=source,
    )


def inputs(
    *cells: CellInput,
    t: int = MIDNIGHT,
    inventory_wh: int = 0,
    bids_wh: int = 0,
    fills_wh: int = 0,
    band: int = 500,
) -> MarketInputs:
    return MarketInputs(
        market_time=t,
        cells=tuple(cells),
        inventory_wh=inventory_wh,
        inventory_source="held_balances" if inventory_wh else "none",
        open_bid_wh=bids_wh,
        recent_fill_wh=fills_wh,
        onchain_band_bps=band,
    )
