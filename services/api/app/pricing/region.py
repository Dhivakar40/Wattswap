"""Regions: 0.5 degree grid cells computed with integer math from lat_e6 / lon_e6."""

from dataclasses import dataclass
from decimal import Decimal

CELL_E6 = 500_000  # 0.5 degrees in 1e-6 degree units


@dataclass(frozen=True, order=True)
class Cell:
    ilat: int
    ilon: int

    @property
    def id(self) -> str:
        return f"{self.ilat}:{self.ilon}"

    @property
    def center_lat_e6(self) -> int:
        return self.ilat * CELL_E6 + CELL_E6 // 2

    @property
    def center_lon_e6(self) -> int:
        return self.ilon * CELL_E6 + CELL_E6 // 2

    @property
    def center_lat(self) -> Decimal:
        return Decimal(self.center_lat_e6) / Decimal(1_000_000)

    @property
    def center_lon(self) -> Decimal:
        return Decimal(self.center_lon_e6) / Decimal(1_000_000)


def cell_of(lat_e6: int, lon_e6: int) -> Cell:
    """The grid cell containing a point. Floor division, so negatives behave."""
    return Cell(lat_e6 // CELL_E6, lon_e6 // CELL_E6)


def cell_from_id(cell_id: str) -> Cell:
    ilat, ilon = cell_id.split(":")
    return Cell(int(ilat), int(ilon))


@dataclass(frozen=True)
class BoundingBox:
    """A market's region. None on every side means 'everywhere'."""

    min_lat_e6: int | None = None
    max_lat_e6: int | None = None
    min_lon_e6: int | None = None
    max_lon_e6: int | None = None

    def contains(self, lat_e6: int, lon_e6: int) -> bool:
        return (
            (self.min_lat_e6 is None or lat_e6 >= self.min_lat_e6)
            and (self.max_lat_e6 is None or lat_e6 <= self.max_lat_e6)
            and (self.min_lon_e6 is None or lon_e6 >= self.min_lon_e6)
            and (self.max_lon_e6 is None or lon_e6 <= self.max_lon_e6)
        )
